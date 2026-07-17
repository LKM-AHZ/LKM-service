from fastapi import APIRouter

from app.modules.common import ModuleStatus

router = APIRouter(prefix="/boards", tags=["boards"])


@router.get("/status", response_model=ModuleStatus)
async def boards_status() -> ModuleStatus:
    return ModuleStatus(
        module="boards",
        responsibility="Manage subject boards, board applications, board moderators, and board-scoped content feeds.",
        next_steps=[
            "Define board and board application models",
            "Add board approval workflow",
            "Connect posts, columns, questions, and projects to boards",
        ],
    )
