import datetime as dt

import pytest
import requests

from monitor.config import IgnavConfig
from monitor.sources.base import STATUS_ERROR, STATUS_NO_RESULTS, STATUS_OK, SearchQuery
from monitor.sources.ignav import IgnavSource

from .conftest import load_fixture


class FakeResponse:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


QUERY = SearchQuery("EZE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), adults=5, max_stops=1,
                    cabin_class="economy", market="US", currency="USD")


def make_source(responses, **cfg):
    http = FakeHTTP(responses)
    sleeps = []
    src = IgnavSource("test-key-123456", IgnavConfig(**cfg), http=http, sleep=sleeps.append)
    return src, http, sleeps


def test_request_uses_only_documented_fields_and_api_key_header():
    src, http, _ = make_source([FakeResponse(200, load_fixture("ignav_round_trip.json"))])
    src.search_round_trip(QUERY)
    call = http.calls[0]
    assert call["url"] == "https://ignav.com/api/fares/round-trip"
    assert call["headers"]["X-Api-Key"] == "test-key-123456"
    assert call["json"] == {
        "origin": "EZE",
        "destination": "GIG",
        "departure_date": "2027-01-15",
        "return_date": "2027-01-25",
        "adults": 5,
        "max_stops": 1,
        "cabin_class": "economy",
        "market": "US",
        "allow_self_transfer": True,
    }


def test_picks_cheapest_itinerary_within_max_stops():
    src, _, _ = make_source([FakeResponse(200, load_fixture("ignav_round_trip.json"))])
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_OK and out.billable
    q = out.quote
    # ccc333 (1500) tiene 2 escalas a la ida: se descarta.
    assert q.source_ref == "bbb222"
    assert q.price_total == 2100 and q.price_pp == 420
    assert q.stops == 1
    assert q.airlines == ("AR", "G3")
    assert q.currency == "USD"


def test_price_per_person_mode():
    src, _, _ = make_source([FakeResponse(200, load_fixture("ignav_round_trip.json"))], price_is_total=False)
    q = src.search_round_trip(QUERY).quote
    assert q.price_pp == 2100 and q.price_total == 10500


def test_other_currency_is_discarded():
    body = load_fixture("ignav_round_trip.json")
    for it in body["itineraries"]:
        it["price"]["currency"] = "BRL"
    src, _, _ = make_source([FakeResponse(200, body)])
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_NO_RESULTS and out.billable


def test_empty_itineraries_is_no_results():
    src, _, _ = make_source([FakeResponse(200, {"itineraries": []})])
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_NO_RESULTS
    assert out.quote is None


def test_retries_on_424_with_exponential_backoff():
    src, http, sleeps = make_source(
        [FakeResponse(424, {"error": {"code": "x"}}), FakeResponse(503),
         FakeResponse(200, load_fixture("ignav_round_trip.json"))],
        backoff_seconds=2,
    )
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_OK
    assert out.attempts == 3
    assert sleeps == [2, 4]


def test_timeouts_exhaust_retries_without_raising():
    src, http, sleeps = make_source([requests.Timeout("t")] * 4, max_retries=3)
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_ERROR
    assert out.attempts == 4 and len(http.calls) == 4
    assert not out.billable and not out.fatal


def test_400_is_not_retried_and_reports_api_message():
    src, http, _ = make_source([FakeResponse(400, load_fixture("ignav_error_400.json"))])
    out = src.search_round_trip(QUERY)
    assert out.status == STATUS_ERROR and not out.fatal
    assert len(http.calls) == 1
    assert "invalid_airport_code" in out.error


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_and_billing_errors_are_fatal(status):
    src, http, _ = make_source([FakeResponse(status, {"error": {"message": "nope"}})])
    out = src.search_round_trip(QUERY)
    assert out.fatal and len(http.calls) == 1


def test_booking_link():
    src, http, _ = make_source([FakeResponse(200, load_fixture("ignav_booking_links.json"))])
    out = src.booking_link("bbb222")
    assert http.calls[0]["url"] == "https://ignav.com/api/fares/booking-links"
    assert http.calls[0]["json"] == {"ignav_id": "bbb222"}
    assert out.links == [("", "https://example.com/book?id=bbb222&pax=5")]
    assert out.billable


def test_missing_api_key_is_rejected():
    with pytest.raises(ValueError):
        IgnavSource("", IgnavConfig(), http=FakeHTTP([]))


def _option(legs, url):
    return {"legs": legs, "links": [{"provider_name": "X", "provider_type": "airline", "url": url}]}


def test_booking_link_prefers_round_trip_option():
    from monitor.sources.ignav import pick_booking_links
    options = [_option(["inbound"], "https://v"), _option(["outbound", "inbound"], "https://rt"),
               _option(["outbound"], "https://i")]
    assert pick_booking_links(options) == [("", "https://rt")]


def test_booking_link_per_leg_when_no_round_trip_option():
    from monitor.sources.ignav import pick_booking_links
    assert pick_booking_links([_option(["inbound"], "https://v"), _option(["outbound"], "https://i")]) == [
        ("Ida", "https://i"), ("Vuelta", "https://v")]
    assert pick_booking_links([_option(["inbound"], "https://v")]) == [("Ida", None), ("Vuelta", "https://v")]
    assert pick_booking_links([]) == []


def test_excluded_airlines_are_sent_and_filtered():
    body = load_fixture("ignav_round_trip.json")
    # La más barata dentro de 1 escala es bbb222 (AR + G3); si se excluye G3 queda aaa111 (G3) también fuera.
    body["itineraries"][0]["outbound"]["segments"][0]["marketing_carrier_code"] = "AR"
    body["itineraries"][0]["inbound"]["segments"][0]["marketing_carrier_code"] = "AR"
    src, http, _ = make_source([FakeResponse(200, body)])
    q = SearchQuery("EZE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), adults=5, max_stops=1,
                    cabin_class="economy", market="US", currency="USD", airlines_exclude=("G3",))
    out = src.search_round_trip(q)
    assert http.calls[0]["json"]["airlines_exclude"] == ["G3"]
    assert out.quote.source_ref == "aaa111" and out.quote.airlines == ("AR",)


def _with_airports(body):
    """Agrega departure_airport/arrival_airport a los segmentos del fixture (BUE→GIG)."""
    routes = {
        "aaa111": [[("AEP", "GIG")], [("GIG", "AEP")]],
        "bbb222": [[("EZE", "GRU"), ("GRU", "GIG")], [("GIG", "GRU"), ("GRU", "AEP")]],
        "ccc333": [[("EZE", "SCL"), ("SCL", "GRU"), ("GRU", "GIG")], [("GIG", "EZE")]],
    }
    for it in body["itineraries"]:
        for leg, hops in zip(("outbound", "inbound"), routes[it["ignav_id"]]):
            for seg, (dep, arr) in zip(it[leg]["segments"], hops):
                seg["departure_airport"], seg["arrival_airport"] = dep, arr
    return body


def test_city_code_search_reports_real_airports():
    src, http, _ = make_source([FakeResponse(200, _with_airports(load_fixture("ignav_round_trip.json")))])
    q = SearchQuery("BUE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), adults=5, max_stops=1,
                    cabin_class="economy", market="US", currency="USD")
    out = src.search_round_trip(q)
    assert http.calls[0]["json"]["origin"] == "BUE"
    assert out.quote.source_ref == "bbb222"
    assert (out.quote.depart_airport, out.quote.return_airport) == ("EZE", "AEP")


def test_excluded_airport_allowed_as_layover_but_not_as_endpoint():
    q = SearchQuery("BUE", "GIG", dt.date(2027, 1, 15), dt.date(2027, 1, 25), adults=5, max_stops=1,
                    cabin_class="economy", market="US", currency="USD", airports_exclude=("SDU",))
    # bbb222 (la más barata) hace escala en SDU en la ida y en la vuelta: se acepta.
    body = _with_airports(load_fixture("ignav_round_trip.json"))
    body["itineraries"][1]["outbound"]["segments"][0]["arrival_airport"] = "SDU"
    body["itineraries"][1]["outbound"]["segments"][1]["departure_airport"] = "SDU"
    body["itineraries"][1]["inbound"]["segments"][0]["arrival_airport"] = "SDU"
    body["itineraries"][1]["inbound"]["segments"][1]["departure_airport"] = "SDU"
    src, _, _ = make_source([FakeResponse(200, body)])
    assert src.search_round_trip(q).quote.source_ref == "bbb222"

    # Si la ida termina en SDU (o la vuelta sale de SDU), se descarta y queda aaa111.
    for leg, idx, key in (("outbound", -1, "arrival_airport"), ("inbound", 0, "departure_airport")):
        body = _with_airports(load_fixture("ignav_round_trip.json"))
        body["itineraries"][1][leg]["segments"][idx][key] = "SDU"
        src, _, _ = make_source([FakeResponse(200, body)])
        assert src.search_round_trip(q).quote.source_ref == "aaa111", leg
