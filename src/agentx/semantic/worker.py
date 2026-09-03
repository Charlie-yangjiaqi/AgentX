"""Semantic Worker 隔离层（Phase 7.8.2，默认启用）。

Python 无法捕获 tree-sitter native SIGSEGV——唯一可靠隔离是子进程。

架构（常驻 worker，逐文件通信，Windows 兼容 subprocess.Popen）：

    MCP Server / enrich_index
        |
        |  WorkerSession（主进程侧）
        |    ├─ Popen(python -m agentx.semantic.worker --serve)
        |    ├─ stdin  逐行 JSON  {"file","source"}
        |    ├─ stdout 逐行 JSON  {"success","functions",...} / {"success":false,"error"}
        |    └─ 崩溃/超时 → 记录 semantic_worker_crash / semantic_timeout → 重启 worker
        |
        └─ tree-sitter 在 worker 进程内解析（独立内存空间）

原则：
- 单个文件 native crash 永远不杀死 AgentX 主进程
- 失败只产生 errors，不中断 enrich / Index 生成
- 不使用 multiprocessing（避免 Windows spawn 问题）
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from typing import Any

from agentx.semantic.types import FileSemantics

_WORKER_MODULE = "agentx.semantic.worker"


# ---------- 序列化（FileSemantics ↔ JSON） ----------


def _to_dict(sem: FileSemantics) -> dict[str, Any]:
    return {
        "functions": [
            {
                "name": f.name,
                "file": f.file,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "signature": None
                if f.signature is None
                else {
                    "return_type": f.signature.return_type,
                    "parameters": [
                        {"name": p.name, "type": p.type} for p in f.signature.parameters
                    ],
                    "text": f.signature.text,
                },
            }
            for f in sem.functions
        ],
        "structs": [
            {
                "name": s.name,
                "file": s.file,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "members": [
                    {
                        "name": m.name,
                        "type": m.type,
                        "line": m.line,
                        "is_function_pointer": m.is_function_pointer,
                    }
                    for m in s.members
                ],
            }
            for s in sem.structs
        ],
        "enums": [
            {
                "name": e.name,
                "file": e.file,
                "start_line": e.start_line,
                "end_line": e.end_line,
                "members": [
                    {
                        "name": m.name,
                        "line": m.line,
                        "value": m.value,
                        "value_expr": m.value_expr,
                    }
                    for m in e.members
                ],
            }
            for e in sem.enums
        ],
        "macros": [
            {
                "name": m.name,
                "file": m.file,
                "line": m.line,
                "value": m.value,
                "value_expr": m.value_expr,
                "kind": m.kind,
            }
            for m in sem.macros
        ],
        "bindings": list(sem.bindings),
        "field_usage": list(sem.field_usage),
        "errors": list(sem.errors),
    }


def _from_dict(data: dict[str, Any]) -> FileSemantics:
    from agentx.semantic.types import (
        EnumInfo,
        EnumMemberInfo,
        FunctionInfo,
        MacroInfo,
        MemberInfo,
        ParamInfo,
        SignatureInfo,
        StructInfo,
    )

    sem = FileSemantics()
    for d in data.get("functions", []):
        sig = d.get("signature")
        signature = None
        if sig is not None:
            signature = SignatureInfo(
                return_type=sig.get("return_type", ""),
                parameters=[
                    ParamInfo(name=p.get("name", ""), type=p.get("type", ""))
                    for p in sig.get("parameters", [])
                ],
                text=sig.get("text", ""),
            )
        sem.functions.append(
            FunctionInfo(
                name=d.get("name", ""),
                file=d.get("file", ""),
                start_line=d.get("start_line", 0),
                end_line=d.get("end_line", 0),
                signature=signature,
            )
        )
    for d in data.get("structs", []):
        sem.structs.append(
            StructInfo(
                name=d.get("name", ""),
                file=d.get("file", ""),
                start_line=d.get("start_line", 0),
                end_line=d.get("end_line", 0),
                members=[
                    MemberInfo(
                        name=m.get("name", ""),
                        type=m.get("type", ""),
                        line=m.get("line", 0),
                        is_function_pointer=bool(m.get("is_function_pointer", False)),
                    )
                    for m in d.get("members", [])
                ],
            )
        )
    for d in data.get("enums", []):
        sem.enums.append(
            EnumInfo(
                name=d.get("name", ""),
                file=d.get("file", ""),
                start_line=d.get("start_line", 0),
                end_line=d.get("end_line", 0),
                members=[
                    EnumMemberInfo(
                        name=m.get("name", ""),
                        line=m.get("line", 0),
                        value=m.get("value"),
                        value_expr=m.get("value_expr"),
                    )
                    for m in d.get("members", [])
                ],
            )
        )
    for d in data.get("macros", []):
        sem.macros.append(
            MacroInfo(
                name=d.get("name", ""),
                file=d.get("file", ""),
                line=d.get("line", 0),
                value=d.get("value"),
                value_expr=d.get("value_expr"),
                kind=str(d.get("kind", "constant")),
            )
        )
    sem.bindings = [dict(b) for b in data.get("bindings", []) if isinstance(b, dict)]
    sem.field_usage = [dict(u) for u in data.get("field_usage", []) if isinstance(u, dict)]
    sem.errors = list(data.get("errors", []))
    return sem


# ---------- 子进程入口 ----------


def worker_main() -> int:
    """子进程入口（--serve 常驻模式）。

    逐行读取 stdin：{"file": ..., "source": ...}
    逐行输出 stdout：{"success": true, <FileSemantics 字段>} 或 {"success": false, "error": ...}
    """
    serve = "--serve" in sys.argv[1:]
    if serve:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            out = _handle_request(line)
            print(json.dumps(out, ensure_ascii=False), flush=True)
        return 0
    # 一次性模式（兼容旧调用/测试）：首行 {"jobs": [...]}
    payload = json.loads(sys.stdin.read() or "{}")
    results: list[dict[str, Any]] = []
    for job in payload.get("jobs", []):
        results.append(_handle_request(json.dumps(job)))
    print(json.dumps({"results": results}, ensure_ascii=False))
    return 0


def _handle_request(line: str) -> dict[str, Any]:
    try:
        job = json.loads(line)
    except json.JSONDecodeError:
        return {"success": False, "error": "invalid request"}
    file = str(job.get("file", ""))
    source = str(job.get("source", ""))
    try:
        from agentx.semantic.extractor import extract_file_semantics

        sem = extract_file_semantics(file, source)
        out = _to_dict(sem)
        out["success"] = True
        return out
    except Exception as e:  # 单文件解析异常：记录并返回，worker 不退出
        err_out: dict[str, Any] = {"success": False, "error": f"{type(e).__name__}: {e}"}
        err_out["file"] = file
        return err_out


# ---------- 结构化错误（诊断定位） ----------


def structured_error(
    type_: str,
    file: str,
    stage: str,
    reason: str,
    recoverable: bool,
    **extra: Any,
) -> str:
    """构造结构化错误 JSON 字符串：{type, file, stage, reason, recoverable, ...}。

    诊断字段说明：
    - file：出错文件
    - stage：阶段（read/parser/extract/worker）
    - reason：具体原因（native_process_exit/worker_timeout/source_too_large/
      tree_sitter_version_incompatible/...）
    - recoverable：是否可通过重试/修环境恢复
    """
    entry: dict[str, Any] = {
        "type": type_,
        "file": file,
        "stage": stage,
        "reason": reason,
        "recoverable": recoverable,
    }
    entry.update(extra)
    return json.dumps(entry, ensure_ascii=False)


# ---------- 主进程侧：WorkerSession ----------


class SemanticWorkerSession:
    """常驻 worker 会话：Popen + 逐文件同步请求 + 超时/崩溃检测 + 重启。

    extract() 返回 (FileSemantics | None, reason)：
    - reason=None：成功
    - reason="worker_crash"：worker 进程退出（含 native SIGSEGV）
    - reason="timeout"：单文件超时（worker 已终止，需重启）

    Phase 7.9 队列分代隔离：每次 _start 递增 generation，reader 线程
    携带代际标记入队；extract 只消费当前代条目，旧 worker 残留的
    ("eof", None) 不会污染新 worker 的结果（防止连续崩溃误报）。
    """

    def __init__(self, timeout_seconds: float = 30.0) -> None:
        self.timeout = timeout_seconds
        self._generation = 0
        self._queue: queue.Queue[tuple[int, str | None]] = queue.Queue()
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._start()

    def _start(self) -> None:
        # 强制 worker 进程 UTF-8 I/O：Windows 管道默认 GBK+surrogateescape，
        # 与主进程 utf-8 协议不一致会产生 surrogate 乱码
        self._generation += 1
        gen = self._generation
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", _WORKER_MODULE, "--serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        assert self._proc.stdout is not None
        self._reader = threading.Thread(target=self._read_loop, args=(gen,), daemon=True)
        self._reader.start()

    def _read_loop(self, gen: int) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                self._queue.put((gen, line))
        except Exception:
            pass
        self._queue.put((gen, None))  # 该代 worker 死亡（崩溃/退出）

    def extract(self, file: str, source: str) -> tuple[FileSemantics | None, str | None]:
        """同步请求单文件；返回 (结果, 失败原因)。失败时 worker 已不可用（调用方重启）。"""
        if self._proc is None or self._proc.poll() is not None:
            return None, "worker_crash"
        gen = self._generation
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(
                json.dumps({"file": file, "source": source}, ensure_ascii=False) + "\n"
            )
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None, "worker_crash"
        while True:
            try:
                item_gen, line = self._queue.get(timeout=self.timeout)
            except queue.Empty:
                return None, "timeout"
            if item_gen != gen:
                continue  # 旧代残留（前一个已崩溃 worker 的 eof/输出）：丢弃
            if line is None:
                return None, "worker_crash"
            break
        try:
            data = json.loads(line or "{}")
        except json.JSONDecodeError:
            return None, "worker_crash"
        if not data.get("success"):
            reason = structured_error(
                "semantic_worker_error",
                file,
                "extract",
                str(data.get("error", "unknown"))[:200],
                True,
            )
            sem = FileSemantics()
            sem.errors.append(reason)
            return sem, None
        return _from_dict(data), None

    def restart(self) -> None:
        """终止并重启 worker（崩溃/超时后由调用方调用）。"""
        self.close()
        self._start()

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
            except OSError:
                pass
            self._proc = None


def run_jobs_isolated(
    jobs: list[tuple[str, str]],
    *,
    timeout_seconds: float = 30.0,
    max_retries: int = 1,
) -> tuple[list[FileSemantics], list[str]]:
    """常驻 worker 逐文件解析；崩溃/超时文件重试（新 worker），失败才记录错误。

    - 每个文件独立 transaction：失败不影响其他文件
    - 崩溃文件重试 max_retries 次（默认 1），重试仍失败 → 结构化错误 + 继续
    - worker 崩溃/超时 → 重启 worker 后继续后续文件（队列分代隔离防误报）
    """
    results: list[FileSemantics] = []
    errors: list[str] = []
    session = SemanticWorkerSession(timeout_seconds=timeout_seconds)
    try:
        for file, source in jobs:
            sem, reason = session.extract(file, source)
            if reason is not None:
                # retry queue：新 worker 重试（single-file transaction）
                session.restart()
                for _ in range(max_retries):
                    sem, reason = session.extract(file, source)
                    if reason is None:
                        break
                    session.restart()
            if reason == "timeout":
                errors.append(
                    structured_error(
                        "semantic_timeout",
                        file,
                        "parser",
                        "worker_timeout",
                        True,
                        timeout_seconds=timeout_seconds,
                    )
                )
                session.restart()
                sem = None
            elif reason == "worker_crash":
                errors.append(
                    structured_error(
                        "semantic_worker_crash",
                        file,
                        "parser",
                        "native_process_exit",
                        True,
                    )
                )
                session.restart()
                sem = None
            if sem is None:
                results.append(FileSemantics())
            else:
                results.append(sem)
    finally:
        session.close()
    return results, errors


if __name__ == "__main__":
    sys.exit(worker_main())
