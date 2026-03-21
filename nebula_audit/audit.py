from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Sequence
import shutil
import tarfile
import uuid
import zipfile

from .config import AppConfig, WORKSPACE_ROOT, ensure_data_dirs


BASE_SYSTEM_PROMPT = """你是一名严谨的资深代码安全审计专家。

你将收到仓库目录和源码快照。该快照不包含任何本地规则命中、漏洞线索或人工预分析结果，
你必须自行从源码中梳理入口、调用链、数据流、权限边界和关键业务流程，再输出审计结论。

输出要求：
1. 使用中文输出 Markdown。
2. 优先报告可利用、证据充分的漏洞，并按严重级别从高到低排序。
3. 每个问题都要包含：标题、风险级别、影响、证据位置、漏洞成因、利用条件、PoC 或利用思路、修复建议。
4. 如果某项风险证据不足，明确标记为“待验证风险”，不要伪造确定性结论。
5. 关注但不限于：SQL 注入、命令执行、反序列化、SSRF、任意文件读写、鉴权绕过、越权、XSS、路径穿越、硬编码密钥、敏感信息泄露、上传漏洞、逻辑缺陷。
6. 报告最后给出整体结论、优先修复顺序、仍需人工复核的关键链路和原因。
"""

DEFAULT_AUDIT_REQUEST = "请对该项目进行完整代码审计，并给出结构化漏洞报告和可操作的 PoC。"

SKIP_DIR_NAMES = {
    ".git",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "bin",
    "obj",
    "target",
    "vendor",
    ".next",
    ".nuxt",
    ".gradle",
    ".mvn",
}

TEXT_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".php",
    ".rb",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".sql",
    ".xml",
    ".yml",
    ".yaml",
    ".json",
    ".ini",
    ".cfg",
    ".conf",
    ".toml",
    ".env",
    ".properties",
    ".jsp",
    ".jspx",
    ".asp",
    ".aspx",
    ".vue",
    ".html",
    ".htm",
    ".sh",
    ".bat",
    ".ps1",
    ".md",
    ".txt",
}

STRUCTURE_FILE_SCORES = {
    "readme.md": 20,
    "package.json": 20,
    "package-lock.json": 16,
    "pnpm-lock.yaml": 16,
    "yarn.lock": 16,
    "requirements.txt": 20,
    "pyproject.toml": 20,
    "poetry.lock": 16,
    "pipfile": 16,
    "pipfile.lock": 16,
    "pom.xml": 20,
    "build.gradle": 18,
    "build.gradle.kts": 18,
    "settings.gradle": 16,
    "settings.gradle.kts": 16,
    "go.mod": 20,
    "go.sum": 16,
    "cargo.toml": 20,
    "cargo.lock": 16,
    "composer.json": 20,
    "composer.lock": 16,
    "gemfile": 20,
    "gemfile.lock": 16,
    "dockerfile": 18,
    "docker-compose.yml": 18,
    "docker-compose.yaml": 18,
    "manage.py": 18,
    "app.py": 18,
    "main.py": 18,
    "server.py": 18,
    "main.go": 18,
    "main.java": 18,
    "program.cs": 18,
    "startup.cs": 18,
    "web.config": 18,
    "application.yml": 16,
    "application.yaml": 16,
    "application.properties": 16,
}

STRUCTURE_PATH_KEYWORDS = {
    "main": 4,
    "server": 4,
    "app": 3,
    "index": 3,
    "bootstrap": 3,
    "startup": 3,
    "config": 2,
    "route": 2,
    "controller": 2,
    "service": 1,
    "handler": 1,
    "model": 1,
}


class AuditPreparationError(RuntimeError):
    pass


@dataclass
class SnapshotEntry:
    path: str
    text: str
    priority: int
    depth: int
    size: int


@dataclass
class AuditContext:
    project_name: str
    archive_name: str
    snapshot: str
    system_prompt: str
    stats: dict
    workspace_dir: str


def is_supported_archive_path(file_path: str | Path) -> bool:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return False
    return zipfile.is_zipfile(path) or tarfile.is_tarfile(path)


def _safe_extract_zip(archive_path: Path, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    with zipfile.ZipFile(archive_path) as bundle:
        for member in bundle.infolist():
            candidate = (target_dir / member.filename).resolve()
            if not candidate.is_relative_to(target_root):
                raise AuditPreparationError("压缩包包含非法路径，已拒绝解压。")
        bundle.extractall(target_dir)


def _safe_extract_tar(archive_path: Path, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    with tarfile.open(archive_path) as bundle:
        for member in bundle.getmembers():
            candidate = (target_dir / member.name).resolve()
            if not candidate.is_relative_to(target_root):
                raise AuditPreparationError("压缩包包含非法路径，已拒绝解压。")
        bundle.extractall(target_dir)


def _archive_stem(archive_path: Path) -> str:
    name = archive_path.name
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return archive_path.stem


def _workspace_run_id() -> str:
    # Keep extraction folder names short to avoid Windows path-length failures.
    return f"{datetime.now():%m%d%H%M}{uuid.uuid4().hex[:4]}"


def _detect_project_root(extract_root: Path) -> Path:
    entries = [item for item in extract_root.iterdir()]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_root


def _looks_like_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    if b"\x00" in sample:
        return False
    return True


def _should_skip(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return any(part in SKIP_DIR_NAMES for part in relative_parts)


def _iter_source_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if _should_skip(path, root):
            continue
        try:
            if path.stat().st_size > 1_000_000:
                continue
        except OSError:
            continue
        if _looks_like_text(path):
            yield path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _render_tree(paths: Sequence[str], max_items: int = 220) -> str:
    if not paths:
        return "(未提取到可阅读源码文件)"
    visible = list(paths[:max_items])
    if len(paths) > max_items:
        visible.append(f"... 其余 {len(paths) - max_items} 个文件已省略")
    return "\n".join(f"- {item}" for item in visible)


def _path_priority(relative_path: str, text: str) -> int:
    lowered = relative_path.lower()
    file_name = lowered.split("/")[-1]
    score = STRUCTURE_FILE_SCORES.get(file_name, 0)

    for keyword, weight in STRUCTURE_PATH_KEYWORDS.items():
        if keyword in lowered:
            score += weight

    depth = lowered.count("/")
    score += max(0, 6 - depth)

    size = len(text)
    if 200 <= size <= 8000:
        score += 2
    elif size < 200:
        score += 1

    return score


def build_repository_snapshot(root: Path, config: AppConfig) -> tuple[str, dict]:
    all_files: list[str] = []
    entries: list[SnapshotEntry] = []

    for path in _iter_source_files(root):
        relative_path = path.relative_to(root).as_posix()
        text = _read_text(path)
        if not text.strip():
            continue
        all_files.append(relative_path)
        entries.append(
            SnapshotEntry(
                path=relative_path,
                text=text,
                priority=_path_priority(relative_path, text),
                depth=relative_path.count("/"),
                size=len(text),
            )
        )

    entries.sort(key=lambda item: (-item.priority, item.depth, item.size, item.path))
    selected_items = entries[: config.audit_max_files]

    selected_sections = []
    used_chars = 0
    for item in selected_items:
        remaining_budget = config.audit_max_chars - used_chars
        if remaining_budget <= 0:
            break
        excerpt = item.text[: min(config.per_file_char_limit, remaining_budget)].strip()
        if not excerpt:
            continue
        lang = item.path.split(".")[-1] if "." in item.path else ""
        if len(item.text) > len(excerpt):
            excerpt += "\n... [内容已截断]"
        section = f"### {item.path}\n```{lang}\n{excerpt}\n```"
        selected_sections.append(section)
        used_chars += len(excerpt)

    snapshot = "\n\n".join(
        [
            "## 仓库概览",
            f"- 可读源码文件数: {len(all_files)}",
            f"- 快照覆盖文件数: {len(selected_sections)}",
            f"- 快照字符数: {used_chars}",
            "- 说明: 未添加任何本地漏洞线索或规则命中，请模型自行基于源码建立调用链和数据流。",
            "",
            "## 目录预览",
            _render_tree(sorted(all_files)),
            "",
            "## 源码快照",
            "\n\n".join(selected_sections) or "(没有足够的文本源码用于快照构建)",
        ]
    )

    stats = {
        "total_files": len(all_files),
        "selected_files": len(selected_sections),
        "snapshot_chars": used_chars,
    }
    return snapshot, stats


def prepare_audit_context(archive_path: str | Path, config: AppConfig) -> AuditContext:
    ensure_data_dirs()
    archive = Path(archive_path)
    if not archive.exists():
        raise AuditPreparationError("源码压缩包不存在。")
    if not is_supported_archive_path(archive):
        raise AuditPreparationError("当前仅支持 zip 与 tar 系列压缩包。")

    projects_root = WORKSPACE_ROOT / "p"
    projects_root.mkdir(parents=True, exist_ok=True)
    temp_dir = projects_root / _workspace_run_id()
    temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        if zipfile.is_zipfile(archive):
            _safe_extract_zip(archive, temp_dir)
        else:
            _safe_extract_tar(archive, temp_dir)

        project_root = _detect_project_root(temp_dir)
        project_name = project_root.name if project_root != temp_dir else _archive_stem(archive)
        snapshot, stats = build_repository_snapshot(project_root, config)

        if stats["total_files"] == 0:
            raise AuditPreparationError("解压成功，但未发现可读源码文件。")

        system_prompt = BASE_SYSTEM_PROMPT
        if config.enable_codex_mode:
            system_prompt = (
                f"{system_prompt}\n\n"
                "你当前运行在 Codex 风格的本地命令审计模式中。\n"
                "需要补充证据时，可以调用本地命令工具读取当前项目工作目录中的源码。\n"
                "运行环境是 Windows PowerShell，请优先使用 `rg --files`、`rg -n`、"
                "`Get-ChildItem`、`Get-Content`、`Select-String` 等只读命令。\n"
                "允许使用 `curl`、`wget`、`Invoke-WebRequest` 这类联网取证命令，"
                "但不能改写或删除本地文件、不能把下载结果保存到磁盘、也不能启动其他程序。\n"
                "如果证据仍不足，"
                "请明确说明而不是猜测。\n"
            )
        if config.system_prompt_suffix.strip():
            system_prompt = f"{system_prompt}\n附加要求：\n{config.system_prompt_suffix.strip()}\n"

        return AuditContext(
            project_name=project_name,
            archive_name=archive.name,
            snapshot=snapshot,
            system_prompt=system_prompt,
            stats=stats,
            workspace_dir=str(project_root),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _trim_history(messages: Sequence[dict], max_chars: int = 18000) -> List[dict]:
    trimmed: List[dict] = []
    consumed = 0
    for message in reversed(messages):
        content = message.get("content", "")
        if not content:
            continue
        if consumed + len(content) > max_chars and trimmed:
            break
        trimmed.insert(0, {"role": message.get("role", "user"), "content": content[-max_chars:]})
        consumed += min(len(content), max_chars)
        if consumed >= max_chars:
            break
    return trimmed


def build_audit_messages(context: AuditContext, extra_instruction: str) -> List[dict]:
    instruction = extra_instruction.strip() or DEFAULT_AUDIT_REQUEST
    return [
        {"role": "system", "content": context.system_prompt},
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"项目名称：{context.project_name}",
                    f"源码压缩包：{context.archive_name}",
                    "",
                    "以下快照没有附带本地漏洞线索或预分析结果，请完全基于源码内容自行审计。",
                    "请优先梳理入口、路由、控制器、服务层、数据访问层、鉴权链路、文件处理链路和关键业务流。",
                    "输出必须是结构化 Markdown 报告，并包含漏洞列表、PoC、修复建议和总体结论。",
                    "",
                    "用户补充要求：",
                    instruction,
                    "",
                    "源码快照：",
                    context.snapshot,
                ]
            ),
        },
    ]


def build_followup_messages(
    context: AuditContext,
    question: str,
    conversation_history: Sequence[dict],
) -> List[dict]:
    history = _trim_history(conversation_history)
    bootstrap_message = {
        "role": "user",
        "content": "\n".join(
            [
                f"当前项目：{context.project_name}",
                "以下是当前项目的源码快照。该快照不含任何本地审计线索；你后续也必须完全基于这些源码内容回答。",
                "如果证据不足，要明确说明。",
                "",
                context.snapshot,
            ]
        ),
    }
    return [
        {"role": "system", "content": context.system_prompt},
        bootstrap_message,
        *history,
        {"role": "user", "content": question.strip()},
    ]
