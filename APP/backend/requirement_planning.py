"""Stub — requirement_planning is not available in the deployable edition."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SpecFileInfo:
    filename: str
    file_type: str = "spec"
    size_bytes: int = 0
    modified_time: str = ""
    has_output: bool = False
    output_files: List[str] = field(default_factory=list)


class _NullRequirementService:
    def list_spec_files(self) -> List[SpecFileInfo]:
        return []

    def get_latest_content(self, filename: str) -> Dict[str, Any]:
        return {"content": None, "source": "none"}

    def save_file_content(self, filename: str, content: str) -> bool:
        return False

    def upload_file(self, filename: str, content: str) -> bool:
        return False

    def list_templates(self) -> List[Dict]:
        return []

    def get_template_content(self, filename: str) -> Optional[str]:
        return None

    def start_generation(self, **kwargs) -> str:
        return "stub-task-not-available"

    def get_task_status(self, task_id: str) -> Optional[Dict]:
        return None

    def cancel_task(self, task_id: str) -> bool:
        return False

    def list_versions(self, spec_file: str) -> List[Dict]:
        return []

    def get_version_content(self, version_file: str) -> Optional[str]:
        return None

    def get_output_content(self, filename: str) -> Optional[str]:
        return None

    def refine_section(self, **kwargs) -> Optional[str]:
        return None

    def refine_content(self, **kwargs) -> Optional[str]:
        return None


def get_requirement_planning_service(llm_service: Any) -> _NullRequirementService:
    return _NullRequirementService()
