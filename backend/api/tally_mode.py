"""Tally mode switching — toggle between live and mock Tally."""

import logging

from fastapi import APIRouter, Depends

from backend.api.dependencies import get_client
from backend.api.models import TallyModeRequest, TallyModeResponse
from tally_bridge.client import TallyClient

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tally-mode", response_model=TallyModeResponse)
async def get_tally_mode(
    client: TallyClient = Depends(get_client),
) -> TallyModeResponse:
    return TallyModeResponse(mode="mock" if client.mock_mode else "live")


@router.post("/tally-mode", response_model=TallyModeResponse)
async def set_tally_mode(
    body: TallyModeRequest,
    client: TallyClient = Depends(get_client),
) -> TallyModeResponse:
    old_mode = "mock" if client.mock_mode else "live"
    client.mock_mode = body.mode == "mock"
    logger.info("Tally mode switched: %s -> %s", old_mode, body.mode)
    return TallyModeResponse(mode=body.mode)
