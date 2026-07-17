from pydantic import BaseModel


class ModuleStatus(BaseModel):
    module: str
    status: str = "planned"
    responsibility: str
    next_steps: list[str]
