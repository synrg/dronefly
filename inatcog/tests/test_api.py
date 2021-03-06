"""Test inatcog.api."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import MagicMock, patch

from inatcog.api import INatAPI

API_REQUESTS_PATCH = patch("inatcog.api.aiohttp.ClientSession.get")


class AsyncMock:
    def __init__(self, expected_result):
        self.status = 200
        self.expected_result = expected_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *error_info):
        return self

    async def json(self):
        return self.expected_result


# For api calls that support rate-limiting (e.g. api.get_users()):
class AsyncSleep(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncSleep, self).__call__(*args, **kwargs)


SLEEP_PATCH = patch("asyncio.sleep", new_callable=AsyncSleep)


class TestAPI(IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = INatAPI()

    async def test_get_taxa_by_id(self):
        """Test get_taxa by id."""
        expected_result = {"results": [{"name": "Animalia"}]}

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result)
            taxon = await self.api.get_taxa(1)
            self.assertEqual(taxon["results"][0]["name"], "Animalia")

    async def test_get_taxa_by_query(self):
        """Test get_taxa with query terms."""
        expected_result = {"results": [{"name": "Animalia"}]}

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result)
            taxon = await self.api.get_taxa(q="animals")
            self.assertEqual(taxon["results"][0]["name"], "Animalia")

    async def test_get_observation_bounds(self):
        """Test get_observation_bounds."""
        expected_result_1 = {}
        expected_result_2 = {
            "total_bounds": {"swlat": 1, "swlng": 2, "nelat": 3, "nelng": 4}
        }

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result_1)
            self.assertIsNone(await self.api.get_observation_bounds([]))
            self.assertIsNone(await self.api.get_observation_bounds(["1"]))

            mock_get.return_value = AsyncMock(expected_result_2)
            self.assertDictEqual(
                await self.api.get_observation_bounds(["1"]),
                expected_result_2["total_bounds"],
            )

    async def test_get_users_by_id(self):
        """Test get_users by id."""
        expected_result = {"results": [{"id": 545640, "login": "benarmstrong"}]}

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result)
            with SLEEP_PATCH:
                users = await self.api.get_users(545640, refresh_cache=True)
                self.assertEqual(users["results"][0]["login"], "benarmstrong")

    async def test_get_users_by_login(self):
        """Test get_users by login."""
        expected_result = {"results": [{"id": 545640, "login": "benarmstrong"}]}

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result)
            with SLEEP_PATCH:
                users = await self.api.get_users("benarmstrong", refresh_cache=True)
                self.assertEqual(users["results"][0]["login"], "benarmstrong")

    async def test_get_users_by_name(self):
        """Test get_users by name."""
        expected_result = {
            "results": [
                {"id": 545640, "login": "benarmstrong"},
                {"id": 2, "login": "bensomebodyelse"},
            ]
        }

        with API_REQUESTS_PATCH as mock_get:
            mock_get.return_value = AsyncMock(expected_result)
            with SLEEP_PATCH:
                users = await self.api.get_users("Ben Armstrong", refresh_cache=True)
                self.assertEqual(users["results"][1]["login"], "bensomebodyelse")
