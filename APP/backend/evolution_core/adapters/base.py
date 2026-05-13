"""
ModuleAdapter Protocol — Darwin 进化框架的适配器接口
所有 module adapter 必须实现此协议。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from APP.backend.evolution_core.genome import Genome


class ModuleAdapter(Protocol):
    module_id: str

    def read_genome(self) -> "Genome":
        """读取当前模块的基因（从源文件解析槽位值）"""
        ...

    def write_genome(self, genome: "Genome") -> None:
        """将基因写回源文件（通过 ratchet）"""
        ...

    def score(self, eval_set_path: str) -> dict[str, float]:
        """对指定 eval set 跑评分，返回各维度分数"""
        ...

    def build_replay_inputs(self, eval_set_path: str) -> list[dict[str, Any]]:
        """从 eval set 构建 pipeline 输入列表"""
        ...

    def run_pipeline(self, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对一批输入跑模块管线，返回输出列表"""
        ...

    def propose_mutations(
        self, weakest_dim: str, context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """
        针对最弱维度提议突变。
        返回 list of Mutation dict:
          {slot_name, old_value_slice, new_value_slice, rationale}
        """
        ...
