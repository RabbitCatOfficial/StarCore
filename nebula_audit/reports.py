from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List
import json
import re
import uuid

from .config import REPORT_INDEX_PATH, REPORTS_ROOT, ensure_data_dirs


@dataclass
class ReportRecord:
    report_id: str
    title: str
    project_name: str
    archive_name: str
    created_at: str
    model: str
    provider: str
    file_path: str
    summary: str

    @classmethod
    def from_dict(cls, payload: dict) -> "ReportRecord":
        return cls(
            report_id=payload.get("report_id", ""),
            title=payload.get("title", ""),
            project_name=payload.get("project_name", ""),
            archive_name=payload.get("archive_name", ""),
            created_at=payload.get("created_at", ""),
            model=payload.get("model", ""),
            provider=payload.get("provider", ""),
            file_path=payload.get("file_path", ""),
            summary=payload.get("summary", ""),
        )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", value).strip("_")
    return slug[:48] or "report"


def _summarize_markdown(content: str) -> str:
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip().strip("#*- ")
        if line:
            lines.append(line)
        if len(" ".join(lines)) >= 200:
            break
    summary = " ".join(lines)
    return summary[:220] if summary else "暂无摘要"


class ReportRepository:
    def __init__(self) -> None:
        ensure_data_dirs()
        self.reports_dir = REPORTS_ROOT
        self.index_path = REPORT_INDEX_PATH
        if not self.index_path.exists():
            self.index_path.write_text("[]", encoding="utf-8")

    def list_reports(self) -> List[ReportRecord]:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        records = [ReportRecord.from_dict(item) for item in payload]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def save_report(
        self,
        *,
        project_name: str,
        archive_name: str,
        model: str,
        provider: str,
        body_markdown: str,
    ) -> ReportRecord:
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        report_id = f"{timestamp}_{uuid.uuid4().hex[:8]}"
        title = f"{project_name} 审计报告"
        file_name = f"{timestamp}_{_slugify(project_name)}.md"
        file_path = self.reports_dir / file_name
        metadata_block = "\n".join(
            [
                f"# {title}",
                "",
                f"- 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 项目名称: {project_name}",
                f"- 源码压缩包: {archive_name}",
                f"- 模型: {model}",
                f"- 服务商: {provider}",
                "",
            ]
        )
        document = metadata_block + body_markdown.strip() + "\n"
        file_path.write_text(document, encoding="utf-8")

        record = ReportRecord(
            report_id=report_id,
            title=title,
            project_name=project_name,
            archive_name=archive_name,
            created_at=now.isoformat(timespec="seconds"),
            model=model,
            provider=provider,
            file_path=str(file_path),
            summary=_summarize_markdown(body_markdown),
        )

        records = self.list_reports()
        records.insert(0, record)
        self.index_path.write_text(
            json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return record

    def read_report(self, record: ReportRecord) -> str:
        path = Path(record.file_path)
        if not path.exists():
            return (
                f"# {record.title}\n\n"
                "该报告文件已不存在，可能被手动移动或删除。\n\n"
                f"- 记录时间: {record.created_at}\n"
                f"- 预期路径: {record.file_path}\n"
            )
        return path.read_text(encoding="utf-8")
