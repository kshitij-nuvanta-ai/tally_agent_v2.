"""Companies endpoint."""

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.dependencies import get_client, get_current_user
from backend.api.models import CompaniesResponse, CompanyItem
from tally_bridge.client import TallyClient
from tally_bridge.exceptions import TallyConnectionError, TallyResponseError
from tally_bridge.queries.masters import list_companies

router = APIRouter()


@router.get("/companies", response_model=CompaniesResponse)
async def get_companies(
    host: str | None = None,
    port: int = Query(9000, ge=1, le=65535),
    mock: bool = False,
    client: TallyClient = Depends(get_client),
    _user: str = Depends(get_current_user),
) -> CompaniesResponse:
    """List Tally companies.

    Without params: uses the app-level client (existing behavior).
    With mock=true: answers from the built-in mock handler.
    With host[/port]: probes that Tally instance ad hoc (used by ConnectCompanyModal).
    """
    ad_hoc: TallyClient | None = None
    if mock:
        ad_hoc = TallyClient()
        ad_hoc.mock_mode = True
    elif host:
        ad_hoc = TallyClient(host, port)
    try:
        companies = await list_companies(ad_hoc or client)
        return CompaniesResponse(
            companies=[CompanyItem(name=c.name) for c in companies],
        )
    except (TallyConnectionError, TallyResponseError) as e:
        raise HTTPException(status_code=503, detail=str(e))
    finally:
        if ad_hoc:
            await ad_hoc.close()
