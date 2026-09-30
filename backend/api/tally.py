"""Tally connectivity endpoints (Group B, Task 8)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.dependencies import get_current_user

router = APIRouter(prefix="/api/tally", tags=["tally"])


class TestConnectionRequest(BaseModel):
    host: str = "localhost"
    port: int = 9000


class TestConnectionResponse(BaseModel):
    connected: bool
    companies: list[str] = []
    error: str | None = None


@router.post("/test-connection", response_model=TestConnectionResponse)
async def test_connection(
    req: TestConnectionRequest,
    user_id: str = Depends(get_current_user),
) -> TestConnectionResponse:
    """Test connectivity to a Tally instance and return its loaded companies.

    Populates the ConnectCompanyModal dropdown (Task 11). Builds a TallyClient
    for the requested host/port and calls the verified ``get_company_list``
    probe (probe E7). Any connection/parse error is returned as
    ``connected=False`` with the error message rather than raising.
    """
    from tally_bridge.client import TallyClient
    from tally_bridge.queries.masters import get_company_list

    client = TallyClient(host=req.host, port=req.port)
    try:
        companies = await get_company_list(client)
        return TestConnectionResponse(connected=True, companies=companies)
    except Exception as e:  # noqa: BLE001 - report any failure to the caller
        return TestConnectionResponse(connected=False, error=str(e))
    finally:
        await client.close()
