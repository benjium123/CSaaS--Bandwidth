import httpx
import pytest

from app.errors import FeatureUnavailableError
from app.providers.numbers import NumberSearch
from app.providers.telnyx.adapter import TelnyxMessagingCarrier


@pytest.mark.parametrize("case", ["locations", "empty", "unauthorized"])
async def test_telnyx_inventory_results(case):
    def handle(request):
        if case == "empty":
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "code": "10031",
                            "detail": (
                                "No numbers found for the given filters. "
                                "Please try again with best_effort=true."
                            ),
                        }
                    ]
                },
            )
        if case == "unauthorized":
            return httpx.Response(401, json={"errors": []})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "phone_number": "+15125550100",
                        "region_information": [
                            {"region_type": "state", "region_name": "TX"},
                            {"region_type": "location", "region_name": "AUSTIN"},
                        ],
                        "features": [{"name": "voice"}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = TelnyxMessagingCarrier(api_key="test", client=client)
        if case == "unauthorized":
            with pytest.raises(FeatureUnavailableError):
                await provider.search_numbers(NumberSearch(area_code="512"))
        else:
            rows = await provider.search_numbers(NumberSearch(area_code="512"))
            if case == "empty":
                assert rows == []
            else:
                assert rows[0].locality == "AUSTIN"
                assert rows[0].region == "TX"
                assert rows[0].capabilities["voice"] is True
