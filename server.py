"""Linnworks MCP server: connects Claude to your own Linnworks account."""
from __future__ import annotations

__version__ = "1.69.5"

import json
import os
import sys
import time
import uuid
import difflib
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import re

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

load_dotenv()


AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"

DEFAULT_LOCATION_ID = "00000000-0000-0000-0000-000000000000"

APPLICATION_ID = os.environ.get("LINNWORKS_APPLICATION_ID")
APPLICATION_SECRET = os.environ.get("LINNWORKS_APPLICATION_SECRET")
INSTALLATION_TOKEN = os.environ.get("LINNWORKS_INSTALLATION_TOKEN")


def _env(name: str) -> str | None:
    value = (os.environ.get(name) or "").strip()
    if not value or value.startswith("${"):
        return None
    return value


DEFAULT_SHOPIFY_SUB_SOURCE = (
    _env("LINNWORKS_DEFAULT_SHOPIFY_SUB_SOURCE") or ""
)

def _require_credentials() -> None:
    if not all([APPLICATION_ID, APPLICATION_SECRET, INSTALLATION_TOKEN]):
        sys.stderr.write(
            "ERROR: Missing Linnworks credentials. Set LINNWORKS_APPLICATION_ID, "
            "LINNWORKS_APPLICATION_SECRET, and LINNWORKS_INSTALLATION_TOKEN — either in "
            "your Claude Desktop config's 'env' block, or in a .env file alongside "
            "this script for local testing.\n"
        )
        sys.exit(1)


_ORDER_STATUS_LABELS: dict[int, str] = {
    0: "Draft",
    1: "Pending Dispatch",
    2: "Paid",
    3: "Return",
    4: "Awaiting Payment",
    5: "Resolution Required",
    6: "Deleted",
    7: "Cancelled",
}

_PAYMENT_STATUS_LABELS: dict[int, str] = {
    0: "UNPAID",
    1: "PAID",
    2: "RETURN",
    3: "PENDING",
    4: "RESEND",
}

_ORDER_STATUS_ACTIONS: dict[str, tuple[str, object]] = {
    "lock":   ("LockOrder", True),
    "unlock": ("LockOrder", False),
    "paid":   ("ChangeStatus", 1),
    "unpaid": ("ChangeStatus", 0),
}

_UNSUPPORTED_STATUS_ACTIONS: set[str] = {"park", "unpark"}

UNPAID_FLIP_PARKS_ORDER: dict[str, Optional[str]] = {
    "state":      "not_observed",
    "evidence":   None,
    "conditions": None,
}


def _unpaid_parks_prediction() -> tuple[Optional[bool], str]:
    state = UNPAID_FLIP_PARKS_ORDER["state"]
    evidence = UNPAID_FLIP_PARKS_ORDER["evidence"]
    conditions = UNPAID_FLIP_PARKS_ORDER["conditions"]

    provenance = ""
    if evidence:
        provenance += f" Observed: {evidence}."
    if conditions:
        provenance += f" Conditions: {conditions}."

    if state == "parks":
        return True, (
            "The order WILL BE PARKED and pulled out of the dispatch queue."
            + provenance
        )
    if state == "does_not_park":
        return False, (
            "The order WILL NOT BE PARKED — it stays in the dispatch queue."
            + provenance
        )
    return None, (
        "⚠️ NOT YET OBSERVED whether flipping an existing order to unpaid parks "
        "it. Linnworks force-parks an order CREATED unpaid (a test order, 18 Sep "
        "2026), but that is ChannelOrderAdapter.Save() on a new order, not "
        "Orders/ChangeStatus(0) on an existing one — do not assume it carries "
        "over. If it does park, this order leaves the dispatch queue. Check the "
        "read-back's is_parked, and check the order in the Linnworks UI."
    )

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


_session: requests.Session = requests.Session()
_token: Optional[str] = None
_server: Optional[str] = None


def authorize() -> tuple[str, str]:
    response = _session.post(
        AUTH_URL,
        data={
            "ApplicationId": APPLICATION_ID,
            "ApplicationSecret": APPLICATION_SECRET,
            "Token": INSTALLATION_TOKEN,
        },
        timeout=30,
    )
    response.raise_for_status()
    auth = response.json()
    token = auth.get("Token")
    server = auth.get("Server")
    if not token or not server:
        raise RuntimeError(
            f"Auth response missing Token/Server. Response keys: {list(auth.keys())}"
        )
    return token, server


def ensure_auth() -> tuple[str, str]:
    global _token, _server
    if _token is None or _server is None:
        _token, _server = authorize()
    return _token, _server


def call_linnworks_form(method_path: str, payload: dict, retry_rate_limit: bool = False) -> dict:
    global _token, _server
    attempt = 0
    while True:
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"

        response = _session.post(
            url,
            data=payload,
            headers={"Authorization": token},
            timeout=60,
        )

        if response.status_code == 401:
            _token, _server = None, None
            token, server = ensure_auth()
            url = f"{server.rstrip('/')}/api/{method_path}"
            response = _session.post(
                url,
                data=payload,
                headers={"Authorization": token},
                timeout=60,
            )

        if retry_rate_limit and _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x). This is a quota "
                "failure, NOT a query result — retry later."
            )
        break

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    if not response.text:
        return {}
    return response.json()


_RATE_LIMIT_BACKOFF = (5, 10, 20, 30)


class RateLimitError(Exception):
    pass


def _is_rate_limited(response) -> bool:
    if response.status_code == 429:
        return True
    return "quota exceeded" in (response.text or "").lower()


def _rate_limit_pause(method_path: str, response, attempt: int) -> bool:
    if attempt >= len(_RATE_LIMIT_BACKOFF):
        return False
    delay = _RATE_LIMIT_BACKOFF[attempt]
    retry_after = (response.headers or {}).get("Retry-After")
    if retry_after:
        try:
            delay = max(delay, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    time.sleep(delay)
    return True


def call_linnworks(method_path: str, payload: dict) -> dict:
    global _token, _server
    attempt = 0
    while True:
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"

        response = _session.post(
            url,
            json=payload,
            headers={"Authorization": token},
            timeout=60,
        )

        if response.status_code == 401:
            _token, _server = None, None
            token, server = ensure_auth()
            url = f"{server.rstrip('/')}/api/{method_path}"
            response = _session.post(
                url,
                json=payload,
                headers={"Authorization": token},
                timeout=60,
            )

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x). This is a quota "
                "failure, NOT a missing record — retry later or slow the batch down."
            )
        break

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    if not response.text.strip():
        return {}

    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def call_linnworks_void(method_path: str, payload: dict) -> None:
    global _token, _server
    attempt = 0
    while True:
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"

        response = _session.post(
            url,
            json=payload,
            headers={"Authorization": token},
            timeout=60,
        )

        if response.status_code == 401:
            _token, _server = None, None
            token, server = ensure_auth()
            url = f"{server.rstrip('/')}/api/{method_path}"
            response = _session.post(
                url,
                json=payload,
                headers={"Authorization": token},
                timeout=60,
            )

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x)."
            )
        break

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )


def call_linnworks_get(method_path: str, params: dict | None = None) -> any:
    global _token, _server
    attempt = 0
    while True:
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"

        response = _session.get(
            url,
            params=params,
            headers={"Authorization": token},
            timeout=60,
        )

        if response.status_code == 401:
            _token, _server = None, None
            token, server = ensure_auth()
            url = f"{server.rstrip('/')}/api/{method_path}"
            response = _session.get(
                url,
                params=params,
                headers={"Authorization": token},
                timeout=60,
            )

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x)."
            )
        break

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    return response.json()


import re as _re

_INJECTION_PATTERNS: list[str] = [
    r"(?i)^ignore\s+(previous|all|above|prior)",
    r"(?i)^(system|assistant|user)\s*:",
    r"(?i)<\|.*?\|>",
    r"(?i)\[/?INST\]",
    r"(?i)###\s*(instruction|system|prompt)",
    r"(?i)^you are now\b",
    r"(?i)^forget\s+(everything|all|previous|prior)",
    r"(?i)^new\s+instruction",
    r"(?i)^act as\b",
    r"(?i)^disregard\s+(all|previous|prior|above)",
    r"---+\s*(system|instructions?|prompt)\b",
]

WRITE_THRESHOLDS: dict[str, int] = {
    "set_stock_levels":               25,
    "set_inventory_item_prices":      25,
    "create_or_update_inventory_item": 50,
    "set_extended_properties":        50,
    "delete_extended_properties":     10,
    "set_inventory_item_descriptions": 50,
    "set_inventory_item_titles":      50,
    "set_inventory_item_suppliers":   50,
    "add_inventory_item_images":      100,
    "delete_inventory_item_images":   10,
    "set_inventory_item_image_order": 25,
    "delete_inventory_item":          10,
    "delete_purchase_order":          10,
    "list_to_shopify":                25,
    "refresh_channel_listing":        25,
    "unpublish_channel_listing":      10,
    "delete_dangling_glt_template":    0,
    "repair_channel_listing_images":  10,
    "delist_all_shopify_listings":    10,
    "delist_all_channel_listings":    10,
    "revise_ebay_listing_description": 10,
    "delete_categories":              10,
    "delete_empty_categories":        10,
    "rename_inventory_item_sku":      10,
    "archive_inventory_items":        25,
    "unarchive_inventory_items":      25,
    "accept_shipping_quote":          25,
    "set_order_status":               25,
    "create_order":                   10,
    "generate_pick_waves":            25,
    "remove_orders_from_pick_waves":  25,
    "default":                        25,
}


def _write_guard(
    operation: str,
    items: list,
    confirmed_count: int | None,
    dry_run: bool,
    threshold: int | None = None,
) -> dict | None:
    if threshold is None:
        threshold = WRITE_THRESHOLDS.get(operation, WRITE_THRESHOLDS["default"])

    count = len(items)

    if count <= threshold:
        return None

    if confirmed_count is None:
        return {
            "staged": True,
            "success": False,
            "operation": operation,
            "item_count": count,
            "threshold": threshold,
            "confirmed_count": None,
            "message": (
                f"Batch of {count} items exceeds the {threshold}-item staging "
                f"threshold for '{operation}'. Review the manifest below, then "
                f"call again with confirmed_count={count} to execute."
            ),
        }

    if confirmed_count != count:
        return {
            "staged": False,
            "success": False,
            "operation": operation,
            "item_count": count,
            "threshold": threshold,
            "confirmed_count": confirmed_count,
            "message": (
                f"confirmed_count={confirmed_count} does not match batch "
                f"size={count}. Call again with confirmed_count={count} to proceed."
            ),
        }

    return None


def _check_injection(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    for pattern in _INJECTION_PATTERNS:
        if _re.search(pattern, value.strip()):
            raise ValueError(
                f"Parameter '{field_name}' contains a pattern that looks like "
                f"an injected instruction rather than legitimate product data "
                f"(matched: {pattern!r}). If this is genuine data, rephrase it "
                f"to avoid instruction-like prefixes."
            )


def _format_order_note(n: dict) -> dict:
    internal = n.get("Internal")
    if internal is None:
        internal = n.get("IsInternal")
    return {
        "note_id":    n.get("OrderNoteId") or n.get("pkOrderNoteId"),
        "note":       n.get("Note") or n.get("NoteText"),
        "internal":   internal,
        "created_on": n.get("NoteDate") or n.get("NoteCreatedOn") or n.get("DateCreated"),
        "created_by": n.get("CreatedBy") or n.get("NoteCreatedBy"),
    }


def _note_id_of(n: dict) -> str:
    return n.get("OrderNoteId") or n.get("pkOrderNoteId") or ""


def _format_order_detail(raw: dict) -> dict:
    general = raw.get("GeneralInfo") or {}
    shipping = raw.get("ShippingInfo") or {}
    customer = raw.get("CustomerInfo") or {}
    address = customer.get("Address") or {}
    billing = customer.get("BillingAddress") or {}
    items = raw.get("Items") or []
    totals_raw = raw.get("TotalsInfo") or {}
    return {
        "order_id": raw.get("OrderId"),
        "num_order_id": raw.get("NumOrderId"),
        "processed": raw.get("Processed"),
        "fulfilment_location_id": raw.get("FulfilmentLocationId"),
        "received_date": general.get("ReceivedDate"),
        "status": general.get("Status"),
        "is_parked": general.get("IsParked"),
        "marker": general.get("Marker"),
        "reference_num": general.get("ReferenceNum"),
        "external_reference": general.get("ExternalReference"),
        "source": general.get("Source"),
        "sub_source": general.get("SubSource"),
        "postal_service_name": shipping.get("PostalServiceName"),
        "tracking_number": shipping.get("TrackingNumber"),
        "customer_name": address.get("FullName") or customer.get("ChannelBuyerName") or "",
        "customer_email": address.get("EmailAddress") or "",
        "delivery_address": {
            "full_name": address.get("FullName"),
            "company": address.get("Company"),
            "address1": address.get("Address1"),
            "address2": address.get("Address2"),
            "address3": address.get("Address3"),
            "town": address.get("Town"),
            "region": address.get("Region"),
            "postcode": address.get("PostCode"),
            "country": address.get("Country"),
            "phone": address.get("PhoneNumber"),
            "email": address.get("EmailAddress"),
        },
        "billing_address": {
            "full_name": billing.get("FullName"),
            "company": billing.get("Company"),
            "address1": billing.get("Address1"),
            "address2": billing.get("Address2"),
            "address3": billing.get("Address3"),
            "town": billing.get("Town"),
            "region": billing.get("Region"),
            "postcode": billing.get("PostCode"),
            "country": billing.get("Country"),
            "phone": billing.get("PhoneNumber"),
            "email": billing.get("EmailAddress"),
        },
        "totals": {
            "subtotal":  totals_raw.get("Subtotal"),
            "postage":   totals_raw.get("PostageCost"),
            "tax":       totals_raw.get("Tax"),
            "total":     totals_raw.get("TotalCharge"),
            "currency":  totals_raw.get("Currency"),
            "discount":  totals_raw.get("TotalDiscount"),
        },
        "items": [
            {
                "StockItemId":    i.get("StockItemId"),
                "SKU":            i.get("SKU"),
                "Title":          i.get("Title"),
                "Quantity":       i.get("Quantity"),
                "row_id":         i.get("RowId"),
                "price_per_unit": i.get("PricePerUnit"),
                "cost_inc_tax":   i.get("CostIncTax"),
                "channel_line_id":     i.get("ItemNumber"),
                "channel_line_source": i.get("ItemSource"),
                "channel_sku":         i.get("ChannelSKU"),
            }
            for i in items
        ],
        "notes": [_format_order_note(n) for n in (raw.get("Notes") or [])],
    }


mcp = FastMCP("linnworks")


_READ_ONLY_TOOLS = frozenset({
    "get_open_orders", "find_inventory_item", "search_inventory_items",
    "list_inventory_items", "get_item_relationships", "find_composite_parents",
    "get_order", "get_order_notes", "find_open_orders_for_sku",
    "find_orders_by_reference", "get_shipping_quote", "get_stock_level",
    "get_stock_change_history", "get_processed_orders", "get_locations",
    "get_extended_properties", "get_processed_order_items",
    "find_unlinked_order_lines", "get_pick_waves", "get_pick_wave_users",
    "get_item_bins", "check_orders_pickable", "get_pick_wave_detail",
    "get_category_report", "get_revenue_summary", "get_top_skus",
    "get_component_sales", "get_sales_by_supplier", "get_period_comparison",
    "get_suppliers", "search_purchase_orders", "get_purchase_order", "get_rules",
    "get_rule", "get_import_list", "get_export_list", "get_import", "get_export",
    "get_inventory_item_descriptions", "get_inventory_item_titles",
    "get_inventory_item_suppliers", "get_categories", "find_dangling_glt_templates",
    "get_channel_listings", "get_channel_listings_bulk",
    "get_inventory_item_images", "get_inventory_item_images_bulk",
})

_ADDITIVE_WRITE_TOOLS = frozenset({
    "add_order_note", "create_order", "create_purchase_order",
    "add_purchase_order_note", "add_purchase_order_item",
    "add_inventory_item_images", "create_variation_group",
    "add_variation_group_items", "create_category", "generate_pick_waves",
    "list_to_shopify", "run_export",
})

_DESTRUCTIVE_WRITE_TOOLS = frozenset({
    "set_order_address", "update_order_note", "delete_order_note",
    "delete_order_notes_by_text", "cancel_order", "remove_order_item",
    "relink_order_line", "accept_shipping_quote", "set_order_status",
    "refund_order", "refund_order_lines", "update_pick_wave",
    "remove_orders_from_pick_waves", "update_purchase_order_header",
    "open_purchase_order", "deliver_purchase_order", "update_purchase_order_item",
    "remove_purchase_order_item", "delete_purchase_order", "run_import",
    "create_or_update_inventory_item", "set_stock_levels",
    "set_inventory_item_prices", "set_extended_properties",
    "delete_extended_properties", "set_inventory_item_descriptions",
    "set_inventory_item_titles", "set_inventory_item_suppliers",
    "delete_inventory_item", "rename_inventory_item_sku", "rename_category",
    "delete_categories", "delete_empty_categories", "delete_dangling_glt_template",
    "delete_inventory_item_images", "set_inventory_item_image_order",
    "refresh_channel_listing", "unpublish_channel_listing",
    "delist_all_channel_listings", "delist_all_shopify_listings",
    "archive_inventory_items", "unarchive_inventory_items",
    "revise_ebay_listing_description", "repair_channel_listing_images",
    "run_readonly_query",
})

_TITLE_WORDS = {"sku": "SKU", "glt": "GLT", "ebay": "eBay", "shopify": "Shopify"}


def _tool_title(name: str) -> str:
    words = [_TITLE_WORDS.get(w, w) for w in name.split("_")]
    first = words[0]
    words[0] = first if first in _TITLE_WORDS.values() else first.capitalize()
    return " ".join(words)


def _tool_annotations(name: str) -> ToolAnnotations:
    hits = [
        label for label, names in (
            ("read-only", _READ_ONLY_TOOLS),
            ("additive", _ADDITIVE_WRITE_TOOLS),
            ("destructive", _DESTRUCTIVE_WRITE_TOOLS),
        ) if name in names
    ]
    if len(hits) != 1:
        raise RuntimeError(
            f"Tool '{name}' must be classified in exactly one of _READ_ONLY_TOOLS, "
            f"_ADDITIVE_WRITE_TOOLS or _DESTRUCTIVE_WRITE_TOOLS (found: {hits or 'none'})"
        )
    read_only = hits[0] == "read-only"
    return ToolAnnotations(
        title=_tool_title(name),
        readOnlyHint=read_only,
        destructiveHint=None if read_only else hits[0] == "destructive",
        openWorldHint=True,
    )


def _tool():
    def decorator(fn):
        ann = _tool_annotations(fn.__name__)
        return mcp.tool(title=ann.title, annotations=ann)(fn)
    return decorator


@_tool()
def get_open_orders(
    location_id: str = DEFAULT_LOCATION_ID,
    limit: int = 50,
    overdue_only: bool = False,
) -> dict:
    """
    List currently open (unprocessed) orders from Linnworks.

    Returns a summary of each order including its IDs, status (decoded),
    dispatch deadline, overdue flag, and the SKUs it contains.

    Useful for answering questions like "how many open orders do I have right
    now?", "which orders are overdue?", or "what SKUs are in today's queue?".

    Args:
        location_id: The Linnworks location to query. Defaults to the "Default"
            location, which represents combined stock for most setups.
        limit: Maximum number of orders to return in detail. Defaults to 50.
            The total count and overdue_count are always returned regardless.
        overdue_only: If True, only return orders whose DispatchBy deadline
            has already passed. Defaults to False (return all open orders).

    Returns:
        A dict with:
          - count:          total number of open orders at the location
          - overdue_count:  number whose DispatchBy has already passed
          - returned:       how many are detailed in the `orders` list
          - location_id:    the location queried
          - orders:         list of order summaries, each with is_overdue flag
    """
    payload = {"request": {"LocationId": location_id}}
    response = call_linnworks("OpenOrders/GetOrdersLowFidelity", payload)

    raw_orders = response.get("Orders") or []
    now = datetime.now(timezone.utc)

    def _is_overdue(dispatch_by: str | None) -> bool:
        if not dispatch_by:
            return False
        try:
            dt = datetime.fromisoformat(dispatch_by.replace("Z", "+00:00"))
            return dt < now
        except ValueError:
            return False

    overdue_count = sum(1 for o in raw_orders if _is_overdue(o.get("DispatchBy")))

    candidates = [o for o in raw_orders if not overdue_only or _is_overdue(o.get("DispatchBy"))]

    summarized = []
    for o in candidates[:limit]:
        items = o.get("Items") or []
        status_code = o.get("Status")
        dispatch_by = o.get("DispatchBy")
        summarized.append({
            "pkOrderID": o.get("pkOrderID"),
            "OrderId": o.get("OrderId"),
            "ReferenceNum": o.get("ReferenceNum"),
            "ExternalReference": o.get("ExternalReference"),
            "Status": status_code,
            "StatusLabel": _ORDER_STATUS_LABELS.get(status_code, f"Unknown({status_code})"),
            "PostalTrackingNumber": o.get("PostalTrackingNumber"),
            "OrderDate": o.get("OrderDate"),
            "DispatchBy": dispatch_by,
            "IsOverdue": _is_overdue(dispatch_by),
            "ItemCount": len(items),
            "SKUs": [i.get("SKU") for i in items],
        })

    return {
        "count": len(raw_orders),
        "overdue_count": overdue_count,
        "returned": len(summarized),
        "location_id": location_id,
        "orders": summarized,
    }


@_tool()
def find_inventory_item(
    sku_or_title: str,
    limit: int = 10,
) -> dict:
    """
    Look up a single inventory item in Linnworks by its exact SKU.

    Returns the item's stock item ID, SKU, title, barcode, retail price, and
    purchase price. Use this to resolve a known SKU to a StockItemId before
    calling stock-level or write tools that require a GUID.

    NOTE: The underlying Linnworks endpoint (GetInventoryItem) only supports
    exact SKU lookup — it does not search by title or accept partial SKUs.
    If you need to search by keyword or browse the catalogue, that capability
    is not available via this API in the current tenant.

    Args:
        sku_or_title: The exact SKU / item number to look up (case-insensitive).
            Title keywords and partial SKUs will not match.
        limit: Unused — kept for interface compatibility. Only one item is ever
            returned by an exact SKU lookup.

    Returns:
        A dict with:
          - query:    the search string used
          - count:    1 if found, 0 if not
          - items:    list containing the matched item (empty if not found)
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku_or_title})
    except RuntimeError:
        return {"query": sku_or_title, "count": 0, "items": [], "note": "No item found for that SKU."}

    return {
        "query": sku_or_title,
        "count": 1,
        "items": [
            {
                "StockItemId": item.get("StockItemId"),
                "SKU": item.get("ItemNumber"),
                "Title": item.get("ItemTitle"),
                "Barcode": item.get("BarcodeNumber"),
                "IsCompositeParent": item.get("IsCompositeParent", False),
                "IsVariationParent": item.get("IsVariationParent", False),
                "RetailPrice": item.get("RetailPrice"),
                "PurchasePrice": item.get("PurchasePrice"),
            }
        ],
    }


@_tool()
def search_inventory_items(
    keyword: str,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """
    Search the inventory catalogue by a free-text keyword.

    This is the discovery counterpart to find_inventory_item. Unlike that tool
    (which only matches an EXACT SKU), this matches the keyword against item
    TITLE, SKU, and BARCODE — the same search the Linnworks inventory search
    box uses. Use it to resolve a human-readable product name (or a partial SKU
    / barcode) to a SKU and StockItemId, so the result can chain straight into
    stock-level, price, or write tools without a second lookup.

    Example: search_inventory_items("Aviator") returns every Aviator variant
    with its SKU and stock_item_id, so "reduce stock of the silver Aviator
    by one" becomes actionable without guessing the SKU string.

    Results are paged — keyword searches can return many rows. Use `page` to
    walk through them; `total_entries` / `total_pages` in the response tell you
    how many there are.

    Args:
        keyword: Free-text term matched against title, SKU, and barcode
            (case-insensitive, partial matches allowed).
        page: 1-based page number to return (default 1).
        per_page: Items per page (default 50, capped at 200).

    Returns:
        A dict with:
          - query:         the keyword searched
          - page:          the page returned
          - per_page:      items per page used
          - total_entries: total matching items across all pages
          - total_pages:   total number of pages
          - count:         number of items in this page
          - items:         list of matched items, each with sku, stock_item_id,
                           title, stock_level, available, in_order, barcode,
                           retail_price, purchase_price, category,
                           is_composite_parent, is_variation_parent
    """
    per_page = max(1, min(per_page, 200))
    page = max(1, page)

    result = call_linnworks_get(
        "Stock/GetStockItems",
        {
            "keyWord": keyword,
            "entriesPerPage": per_page,
            "pageNumber": page,
        },
    )

    data = result.get("Data", []) if isinstance(result, dict) else []
    items = [
        {
            "sku": row.get("ItemNumber"),
            "stock_item_id": row.get("StockItemId"),
            "title": row.get("ItemTitle"),
            "stock_level": row.get("Quantity"),
            "available": row.get("Available"),
            "in_order": row.get("InOrder"),
            "barcode": row.get("BarcodeNumber"),
            "retail_price": row.get("RetailPrice"),
            "purchase_price": row.get("PurchasePrice"),
            "category": row.get("CategoryName"),
            "is_composite_parent": row.get("IsCompositeParent", False),
            "is_variation_parent": row.get("IsVariationParent", False),
        }
        for row in data
    ]

    return {
        "query": keyword,
        "page": result.get("PageNumber", page) if isinstance(result, dict) else page,
        "per_page": result.get("EntriesPerPage", per_page) if isinstance(result, dict) else per_page,
        "total_entries": result.get("TotalEntries", len(items)) if isinstance(result, dict) else len(items),
        "total_pages": result.get("TotalPages", 1) if isinstance(result, dict) else 1,
        "count": len(items),
        "items": items,
    }


_STOCK_LEVEL_SUM_FIELDS = {
    "quantity": "StockLevel",
    "available": "Available",
    "in_order":  "InOrders",
    "due":       "Due",
}


def _fetch_full_stock_page(
    page: int,
    per_page: int,
    include_composite_parents: bool,
    include_variation_parents: bool,
    data_requirements: list[str],
) -> list | None:
    payload = {
        "keyword": "",
        "loadCompositeParents": include_composite_parents,
        "loadVariationParents": include_variation_parents,
        "entriesPerPage": per_page,
        "pageNumber": page,
        "dataRequirements": data_requirements,
        "searchTypes": [],
    }
    for _ in range(6):
        try:
            resp = call_linnworks("Stock/GetStockItemsFull", payload)
            return resp if isinstance(resp, list) else []
        except RuntimeError as exc:
            msg = str(exc)
            if "No items found with given filter" in msg:
                return None
            if "429" in msg or "quota" in msg.lower():
                time.sleep(15)
                continue
            raise
    raise RuntimeError(
        f"Inventory sweep aborted: repeated rate-limit (429) on page {page}. "
        "No partial result is returned — a truncated sweep would under-report "
        "items and could wrongly clear stock as dead."
    )


def _format_full_stock_row(
    row: dict,
    location_id: str | None,
    stock_levels_loaded: bool,
) -> dict:
    levels = row.get("StockLevels") or []
    if location_id:
        wanted = location_id.strip().lower()
        levels = [
            l for l in levels
            if ((l.get("Location") or {}).get("StockLocationId") or "").lower() == wanted
        ]

    if not stock_levels_loaded:
        levels = []
        totals = {k: None for k in _STOCK_LEVEL_SUM_FIELDS}
    else:
        totals = {
            key: sum((l.get(field) or 0) for l in levels)
            for key, field in _STOCK_LEVEL_SUM_FIELDS.items()
        }

    return {
        "sku": row.get("ItemNumber"),
        "stock_item_id": row.get("StockItemId"),
        "title": row.get("ItemTitle"),
        "barcode": row.get("BarcodeNumber"),
        "category_name": row.get("CategoryName"),
        "category_id": row.get("CategoryId"),
        "purchase_price": row.get("PurchasePrice"),
        "quantity": totals["quantity"],
        "available": totals["available"],
        "in_order": totals["in_order"],
        "due": totals["due"],
        "is_variation_parent": row.get("IsVariationParent", False),
        "is_composite_parent": None,
        "locations": [
            {
                "location_id": (l.get("Location") or {}).get("StockLocationId"),
                "location_name": (l.get("Location") or {}).get("LocationName"),
                "stock_level": l.get("StockLevel"),
                "available": l.get("Available"),
                "in_order": l.get("InOrders"),
                "due": l.get("Due"),
                "minimum_level": l.get("MinimumLevel"),
            }
            for l in levels
        ],
    }


@_tool()
def list_inventory_items(
    page: int = 1,
    per_page: int = 200,
    all_pages: bool = False,
    include_stock_levels: bool = True,
    location_id: str | None = None,
    zero_stock_only: bool = False,
    include_composite_parents: bool = True,
    include_variation_parents: bool = True,
    flag_composite_parents: bool = False,
    max_items: int = 5000,
) -> dict:
    """
    Enumerate ACTIVE inventory items with their stock levels: the "give me
    everything" counterpart to search_inventory_items (keyword) and
    find_inventory_item (exact SKU). Typical use is a catalogue cleanup: find
    what is out of stock everywhere, then check it with find_composite_parents
    before delist_all_channel_listings and archive_inventory_items.

    Stock is per location. With include_stock_levels=True each item carries a
    `locations` breakdown, and `quantity`/`available`/`in_order`/`due` are summed
    across all locations. Stock can sit outside Default (e.g. supplier-mirror
    locations), so a Default-only zero is not dead stock. Pass `location_id` to
    measure one location instead.

    ⚠️ ACTIVE ITEMS ONLY. Archived items are never returned, and no endpoint lists
    them (only a UI/Data export).

    ⚠️ COST: one call per 200 items. all_pages=True sweeps the whole catalogue
    (roughly 170 calls for 34k items) and must never run in parallel with another
    sweeping tool (get_top_skus, find_composite_parents, …); they rate-limit each
    other.

    Args:
        page: 1-based page (ignored when all_pages=True).
        per_page: Items per page; 200 is the maximum.
        all_pages: Sweep the whole catalogue. Pair with zero_stock_only / max_items.
        include_stock_levels: Default True. False is faster, but quantities come
            back None, meaning "not read", NOT zero.
        location_id: Measure one location only (needs include_stock_levels).
        zero_stock_only: Return only items at zero in the scope measured (all
            locations, or `location_id`). Needs include_stock_levels.
        include_composite_parents: Default True. False makes bundle parents ABSENT
            from the results (they hold no stock of their own), so the list is no
            longer the full catalogue.
        include_variation_parents: Same, for variation parents.
        flag_composite_parents: Fill `is_composite_parent`, which this endpoint
            doesn't return. Costs about 215 calls to build the composite index the
            first time (then cached 15 minutes). Left False, it stays None, meaning
            "not determined". `is_variation_parent` is always real.
        max_items: Cap on rows returned (default 5000). The sweep and the counts
            still cover everything; only `items` is cut, with `truncated: true`.

    Returns:
        A dict with scope, pages_fetched, scanned_count, matched_count, count,
        truncated, complete, and items (sku, stock_item_id, title, barcode,
        category_name, category_id, purchase_price, quantity, available,
        in_order, due, is_variation_parent, is_composite_parent, locations).
    """
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    max_items = max(1, max_items)

    if location_id and not include_stock_levels:
        raise ValueError(
            "location_id requires include_stock_levels=True — quantities are "
            "derived from the per-location StockLevels array."
        )
    if zero_stock_only and not include_stock_levels:
        raise ValueError(
            "zero_stock_only requires include_stock_levels=True — with stock "
            "levels unread, quantity is None (not read) and cannot be tested "
            "for zero."
        )

    data_requirements = ["StockLevels"] if include_stock_levels else []

    items: list[dict] = []
    scanned = 0
    matched = 0
    pages_fetched = 0
    truncated = False
    current = page

    while True:
        rows = _fetch_full_stock_page(
            current, per_page,
            include_composite_parents, include_variation_parents,
            data_requirements,
        )
        if rows is None:
            break
        pages_fetched += 1
        scanned += len(rows)

        for row in rows:
            formatted = _format_full_stock_row(row, location_id, include_stock_levels)
            if zero_stock_only and formatted["quantity"] != 0:
                continue
            matched += 1
            if len(items) < max_items:
                items.append(formatted)
            else:
                truncated = True

        if not all_pages or not rows or len(rows) < per_page:
            break
        current += 1
        time.sleep(0.42)

    if flag_composite_parents and items:
        parents = _get_composite_index().get("parents") or {}
        for it in items:
            sid = (it.get("stock_item_id") or "").lower()
            it["is_composite_parent"] = sid in parents

    return {
        "scope": {
            "active_only": True,
            "location_id": location_id,
            "stock_levels_loaded": include_stock_levels,
            "zero_stock_only": zero_stock_only,
            "includes_composite_parents": include_composite_parents,
            "includes_variation_parents": include_variation_parents,
            "composite_parent_flag_resolved": flag_composite_parents,
        },
        "page": None if all_pages else page,
        "per_page": per_page,
        "pages_fetched": pages_fetched,
        "scanned_count": scanned,
        "matched_count": matched,
        "count": len(items),
        "truncated": truncated,
        "complete": all_pages,
        "items": items,
    }


def _format_variation_member(m: dict) -> dict:
    return {
        "sku": m.get("ItemNumber") or m.get("SKU"),
        "stock_item_id": m.get("pkStockItemId") or m.get("StockItemId"),
        "title": m.get("ItemTitle"),
    }


def _find_variation_group_by_name(name: str) -> dict | None:
    target = (name or "").strip().lower()
    if not target:
        return None
    page: int | None = 1
    pages_scanned = 0
    while page and pages_scanned < 10:
        res = call_linnworks_get(
            "Stock/SearchVariationGroups",
            {"searchType": "VariationName", "searchText": name,
             "pageNumber": page, "entriesPerPage": 100},
        )
        data = res.get("Data", []) if isinstance(res, dict) else []
        total_pages = res.get("TotalPages", 1) if isinstance(res, dict) else 1
        for row in data:
            if (row.get("VariationGroupName") or "").strip().lower() == target:
                return row
        pages_scanned += 1
        page = page + 1 if page < total_pages else None
    return None


def _resolve_variation(sku: str, stock_item_id: str) -> dict:
    group = call_linnworks_get(
        "Stock/GetVariationGroupByParentId", {"pkStockItemId": stock_item_id}
    )
    if group:
        pk = group.get("pkVariationItemId")
        members = call_linnworks_get(
            "Stock/GetVariationItems", {"pkVariationItemId": pk}
        ) or []
        return {
            "role": "parent",
            "group_name": group.get("VariationGroupName"),
            "parent_sku": sku,
            "parent_stock_item_id": stock_item_id,
            "children": [_format_variation_member(m) for m in members],
            "siblings": [],
            "reverse_lookup_confirmed": True,
        }

    target = (sku or "").strip().lower()
    page: int | None = 1
    pages_scanned = 0
    while page and pages_scanned < 5:
        res = call_linnworks_get(
            "Stock/SearchVariationGroups",
            {"searchType": "ItemSKU", "searchText": sku,
             "pageNumber": page, "entriesPerPage": 100},
        )
        candidates = res.get("Data", []) if isinstance(res, dict) else []
        total_pages = res.get("TotalPages", 1) if isinstance(res, dict) else 1
        for cand in candidates:
            members = call_linnworks_get(
                "Stock/GetVariationItems",
                {"pkVariationItemId": cand.get("pkVariationItemId")},
            ) or []
            member_rows = [_format_variation_member(m) for m in members]
            if any((r["sku"] or "").strip().lower() == target for r in member_rows):
                return {
                    "role": "child",
                    "group_name": cand.get("VariationGroupName"),
                    "parent_sku": cand.get("VariationSKU"),
                    "parent_stock_item_id": cand.get("pkVariationItemId"),
                    "children": [],
                    "siblings": [
                        r for r in member_rows
                        if (r["sku"] or "").strip().lower() != target
                    ],
                    "reverse_lookup_confirmed": True,
                }
        pages_scanned += 1
        page = page + 1 if page < total_pages else None

    return {
        "role": "none",
        "group_name": None,
        "parent_sku": None,
        "children": [],
        "siblings": [],
        "reverse_lookup_confirmed": True,
        "note": "Not part of any variation group (forward + reverse checked).",
    }


def _resolve_composition(sku: str, stock_item_id: str) -> dict:
    comps = call_linnworks_get(
        "Inventory/GetInventoryItemCompositions",
        {"inventoryItemId": stock_item_id, "getFullDetail": "true"},
    ) or []

    if comps:
        return {
            "role": "parent",
            "components": [
                {
                    "sku": c.get("SKU"),
                    "linked_stock_item_id": c.get("LinkedStockItemId"),
                    "title": c.get("ItemTitle"),
                    "quantity": c.get("Quantity"),
                    "purchase_price": c.get("PurchasePrice"),
                }
                for c in comps
            ],
            "belongs_to": [],
            "reverse_lookup_supported": False,
            "reverse_lookup_tool": "find_composite_parents",
        }

    return {
        "role": "none",
        "components": [],
        "belongs_to": [],
        "reverse_lookup_supported": False,
        "reverse_lookup_tool": "find_composite_parents",
        "note": ("Not a composite parent. Whether this SKU is a COMPONENT of "
                 "another composite is NOT resolved by this call (it needs a "
                 "catalogue-wide index) — use find_composite_parents for that, "
                 "especially before archiving/delisting/deleting the SKU."),
    }


@_tool()
def get_item_relationships(sku: str) -> dict:
    """
    Resolve how a SKU relates to parent/child groupings — variation groups AND
    composites (bundles) — in both directions, in a single call.

    This answers the question "does this SKU belong to a parent, and which one?"
    that find_inventory_item and search_inventory_items cannot: those only tell
    you whether a SKU *is* a parent (flags), never what it rolls up under or what
    it contains.

    What it resolves:
      • Variation — forward (parent -> its child variants) and reverse
        (child -> its parent group + sibling variants). Reverse is confirmed
        working, so a child SKU returns its parent and siblings.
      • Composite — forward (bundle/kit parent -> its components, with
        per-component quantity and purchase price). Reverse (component -> the
        bundles that contain it) is NOT resolved here — it needs a catalogue-wide
        index, so it lives in `find_composite_parents`. Use that one before
        archiving/delisting/deleting a SKU that might feed a live bundle.

    Args:
        sku: The exact SKU / item number (case-insensitive). Partial SKUs and
            title keywords will not match — use search_inventory_items first if
            you only have a product name.

    Returns:
        A dict with:
          - sku, stock_item_id, title, found
          - is_variation_parent, is_composite_parent — derived from the live
            relationship endpoints (the raw item flags are unreliable in this
            tenant), not from GetInventoryItem's flags
          - variation: {role ("parent"|"child"|"none"), group_name, parent_sku,
              children[] (when parent), siblings[] (when child),
              reverse_lookup_confirmed}
          - composite: {role ("parent"|"none"), components[] (when parent),
              belongs_to[] (always empty here), reverse_lookup_supported (always
              False here), reverse_lookup_tool ("find_composite_parents")}
        Children/siblings/components each carry sku, stock_item_id/linked id, and
        title; components also carry quantity and purchase_price.

        If the SKU does not exist, returns {sku, found: False, note}.
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {
            "sku": sku,
            "found": False,
            "note": ("No inventory item found for that exact SKU. Use "
                     "search_inventory_items for keyword / partial / barcode lookup."),
        }

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"sku": sku, "found": False,
                "note": "Item found but returned no StockItemId."}

    canonical_sku = item.get("ItemNumber") or sku
    variation = _resolve_variation(canonical_sku, stock_item_id)
    composite = _resolve_composition(canonical_sku, stock_item_id)

    return {
        "sku": canonical_sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "found": True,
        "is_variation_parent": variation["role"] == "parent",
        "is_composite_parent": composite["role"] == "parent",
        "variation": variation,
        "composite": composite,
    }


_COMPOSITE_INDEX_TTL_SECONDS = 900
_composite_index_cache: dict | None = None


def _build_composite_index() -> dict:
    parents: dict[str, dict] = {}
    sku_to_id: dict[str, dict] = {}
    total_items = 0
    page = 1
    sweep_pages = 0

    while True:
        resp = None
        for _ in range(6):
            try:
                resp = call_linnworks_get(
                    "Stock/GetStockItems",
                    {"keyWord": "", "entriesPerPage": 200, "pageNumber": page},
                )
                break
            except RuntimeError as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(15)
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                f"Composite index build aborted: repeated rate-limit (429) on sweep "
                f"page {page}. No partial index is returned — a partial sweep would "
                "under-report composite parents and could wrongly clear a component "
                "as safe to archive."
            )

        data = resp.get("Data") or []
        sweep_pages += 1
        for it in data:
            sid = it.get("StockItemId")
            if not sid:
                continue
            sid = sid.lower()
            total_items += 1
            sku = it.get("ItemNumber")
            title = it.get("ItemTitle")
            if sku:
                sku_to_id[sku.strip().lower()] = {"stock_item_id": sid, "title": title}
            if it.get("IsCompositeParent"):
                parents[sid] = {"sku": sku, "title": title}

        total_pages = resp.get("TotalPages") or 1
        if page >= total_pages or not data:
            break
        page += 1
        time.sleep(0.42)

    index: dict[str, list] = {}
    parent_ids = list(parents)
    composition_calls = 0
    for i in range(0, len(parent_ids), 100):
        chunk = parent_ids[i:i + 100]
        resp = None
        for _ in range(6):
            try:
                resp = call_linnworks(
                    "Inventory/GetInventoryItemsCompositionByIds",
                    {"request": {"InventoryItemIds": chunk}},
                )
                break
            except RuntimeError as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(15)
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                "Composite index build aborted: repeated rate-limit (429) resolving "
                "compositions. No partial index is returned."
            )
        composition_calls += 1
        for pid, comps in (resp.get("InventoryItemsCompositionByIds") or {}).items():
            pid = (pid or "").lower()
            for c in comps or []:
                cid = (c.get("LinkedStockItemId") or "").lower()
                if not cid:
                    continue
                index.setdefault(cid, []).append(
                    {"parent_id": pid, "quantity": c.get("Quantity")}
                )
        time.sleep(0.25)

    return {
        "index":     index,
        "parents":   parents,
        "sku_to_id": sku_to_id,
        "built_at":  time.time(),
        "stats": {
            "active_items":       total_items,
            "composite_parents":  len(parents),
            "indexed_components": len(index),
            "api_calls":          sweep_pages + composition_calls,
            "sweep_pages":        sweep_pages,
            "composition_calls":  composition_calls,
        },
    }


def _get_composite_index(rebuild: bool = False) -> dict:
    global _composite_index_cache
    cache = _composite_index_cache
    if (not rebuild and cache
            and (time.time() - cache["built_at"]) < _COMPOSITE_INDEX_TTL_SECONDS):
        return cache
    _composite_index_cache = _build_composite_index()
    return _composite_index_cache


@_tool()
def find_composite_parents(
    skus: list[str],
    include_listing_status: bool = True,
    max_parents_listed: int = 25,
    rebuild_index: bool = False,
) -> dict:
    """
    For each SKU, find the composite/bundle parents that CONTAIN it — the reverse
    of get_item_relationships' forward parent -> components lookup.

    Answers the archive-safety question the rest of the toolset cannot:
    "before I archive/delist/delete this dead-looking SKU, is it a component of a
    bundle that is still live?" A component can read as dead stock on its own
    while still feeding a selling composite (pooled multipacks, kits,
    custom builds) — retiring it silently breaks the parent.

    Run this as a gate before archive_inventory_items, delete_inventory_item,
    delist_all_channel_listings, or any bulk operation that retires items.

    HOW IT WORKS / COST. Linnworks has no component -> parent endpoint, so this
    enumerates every composite parent in the ACTIVE catalogue and inverts their
    component lists into an index. For a catalogue of 34k items with ~4.3k composite parents
    that is ~215 API calls / ~105 seconds. The index is built ONCE and
    cached for 15 minutes, so pass the whole batch of candidate SKUs in one call
    rather than calling per SKU. Do not run this in parallel with the other
    autopaginating tools.

    ⚠️  ACTIVE ITEMS ONLY. The sweep endpoint never returns archived items, so an
    ARCHIVED composite parent is invisible here. Safe for the archive-gate use
    (an archived parent is not selling), but it is not a complete history.

    Args:
        skus: Exact SKUs / ItemNumbers to screen (case-insensitive). Resolved from
            the sweep itself at no extra cost; anything not in the active
            catalogue falls back to a direct lookup and, failing that, is
            reported under `unresolved` (an archived SKU cannot be resolved by
            SKU at all — that is a Linnworks limitation).
        include_listing_status: Also report whether each parent is live on a sales
            channel (Inventory/BatchGetInventoryItemChannelSKUs, ~1 call per 200
            distinct parents). This is what turns "is a component" into "is a
            component of something still selling". Default True.
        max_parents_listed: Cap on how many parent rows are returned per SKU
            (default 25). Some components sit in thousands of composites, which
            would swamp the response. `parent_count` and `listed_parent_count`
            are always counted across ALL parents, never just the returned ones,
            so the safety verdict is never truncated — only the detail is.
        rebuild_index: Force a fresh index build instead of using the cached one.

    Returns:
        A dict with:
          - item_count, resolved_count
          - component_count: how many resolved SKUs are a component of anything
          - blocked_count: how many have at least one LISTED parent — the SKUs
            you must NOT retire (None-ish when include_listing_status=False)
          - results: per-SKU rows with sku, stock_item_id, title, is_component,
            parent_count, listed_parent_count, has_listed_parent,
            safe_to_retire (False if it feeds a live parent), parents[]
            (parent_sku, parent_stock_item_id, parent_title, quantity,
            parent_is_listed, parent_channels), parents_truncated
          - unresolved: rows for SKUs not found in the active catalogue
          - index: build/cache stats (built_at, age_seconds, from_cache, and the
            sweep totals) so a stale answer is visible rather than implicit
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    was_cached = (
        not rebuild_index
        and _composite_index_cache is not None
        and (time.time() - _composite_index_cache["built_at"]) < _COMPOSITE_INDEX_TTL_SECONDS
    )
    idx = _get_composite_index(rebuild=rebuild_index)
    index, parents, sku_to_id = idx["index"], idx["parents"], idx["sku_to_id"]

    resolved: list[tuple[str, str, str | None]] = []
    unresolved: list[dict] = []
    for raw in skus:
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        hit = sku_to_id.get(s.lower())
        if hit:
            resolved.append((s, hit["stock_item_id"], hit["title"]))
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
            sid = item.get("StockItemId")
            if sid:
                resolved.append((s, sid.lower(), item.get("ItemTitle")))
                continue
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
        except RuntimeError:
            unresolved.append({
                "sku": s,
                "error": ("not found in the active catalogue — it may be archived "
                          "(archived SKUs cannot be resolved by SKU in Linnworks)"),
            })

    listed_ids: set[str] = set()
    channels_by_parent: dict[str, list] = {}
    listing_error: str | None = None
    if include_listing_status:
        distinct_parents = sorted({
            p["parent_id"]
            for _, sid, _ in resolved
            for p in index.get(sid, [])
        })
        if distinct_parents:
            try:
                by_id = _fetch_channel_skus_for_ids(distinct_parents)
                for pid, rows in by_id.items():
                    if rows:
                        listed_ids.add(pid)
                        channels_by_parent[pid] = sorted(
                            {r.get("Source") for r in rows if r.get("Source")}
                        )
            except RuntimeError as exc:
                listing_error = str(exc)

    results: list[dict] = []
    component_count = 0
    blocked_count = 0
    for s, sid, title in resolved:
        links = index.get(sid, [])
        if links:
            component_count += 1

        rows = []
        listed_parent_count = 0
        for link in links:
            pid = link["parent_id"]
            meta = parents.get(pid, {})
            is_listed = (pid in listed_ids) if include_listing_status else None
            if is_listed:
                listed_parent_count += 1
            rows.append({
                "parent_sku":            meta.get("sku"),
                "parent_stock_item_id":  pid,
                "parent_title":          meta.get("title"),
                "quantity":              link.get("quantity"),
                "parent_is_listed":      is_listed,
                "parent_channels":       channels_by_parent.get(pid, []),
            })

        rows.sort(key=lambda r: (not r["parent_is_listed"], r["parent_sku"] or ""))
        has_listed = listed_parent_count > 0
        if has_listed:
            blocked_count += 1

        results.append({
            "sku":                 s,
            "stock_item_id":       sid,
            "title":               title,
            "is_component":        bool(links),
            "parent_count":        len(links),
            "listed_parent_count": listed_parent_count if include_listing_status else None,
            "has_listed_parent":   has_listed if include_listing_status else None,
            "safe_to_retire":      (not has_listed) if include_listing_status else None,
            "parents":             rows[:max_parents_listed],
            "parents_truncated":   len(rows) > max_parents_listed,
        })

    out = {
        "item_count":      len(skus),
        "resolved_count":  len(resolved),
        "component_count": component_count,
        "blocked_count":   blocked_count if include_listing_status else None,
        "results":         results,
        "unresolved":      unresolved,
        "index": {
            "from_cache":  was_cached,
            "built_at":    datetime.fromtimestamp(idx["built_at"], timezone.utc)
                               .isoformat(),
            "age_seconds": round(time.time() - idx["built_at"]),
            "ttl_seconds": _COMPOSITE_INDEX_TTL_SECONDS,
            "scope":       "active items only — archived composite parents are invisible",
            **idx["stats"],
        },
    }
    if listing_error:
        out["listing_status_error"] = (
            f"Parent listing status could not be read ({listing_error}); "
            "parent_is_listed / safe_to_retire are unreliable for this run."
        )
    return out


@_tool()
def get_order(order_id: str) -> dict:
    """
    Fetch full detail for a single Linnworks order.

    Accepts either a GUID pkOrderID (as returned by get_open_orders) or a
    numeric order number (the human-facing NumOrderId). Automatically routes
    to the correct endpoint based on the input format.

    Returns the order's status, received date, postal service, parked flag,
    marker, source channel, and all item lines with SKUs and quantities. Each
    line also carries channel_line_id/channel_line_source/channel_sku — the
    channel's own reference for that line (None only if the API never
    returned the field at all). Use find_unlinked_order_lines to scan many
    orders for lines missing this reference, rather than reading it here one
    order at a time. Useful for answering questions like "show me order
    596475 in full" or "what items are in this order and what's the shipping
    method?".

    Args:
        order_id: Either a GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or a
            numeric order number (e.g. "596475"). Both formats are accepted.

    Returns:
        A dict with order detail including status, shipping, and item lines.
    """
    order_id = order_id.strip()

    if _UUID_RE.match(order_id):
        response = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        if isinstance(response, list):
            orders = response
        else:
            orders = response.get("Orders") or response.get("Data") or []
        if not orders:
            return {"error": f"No order found for GUID '{order_id}'"}
        return _format_order_detail(orders[0])

    else:
        response = call_linnworks_get(
            "Orders/GetOrderDetailsByNumOrderId",
            params={"orderId": order_id},
        )
        if isinstance(response, dict):
            if "GeneralInfo" in response or "NumOrderId" in response:
                return _format_order_detail(response)
            orders = response.get("Orders") or []
            if orders:
                return _format_order_detail(orders[0])
        return {"error": f"No order found for numeric ID '{order_id}'", "raw": response}


@_tool()
def set_order_address(
    order_id: str,
    full_name: Optional[str] = None,
    company: Optional[str] = None,
    address1: Optional[str] = None,
    address2: Optional[str] = None,
    address3: Optional[str] = None,
    town: Optional[str] = None,
    region: Optional[str] = None,
    postcode: Optional[str] = None,
    country: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    save_to_crm: bool = False,
    require_complete: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Update the delivery address on an open (unprocessed) order; processed orders
    are refused. Reads the current address, changes only the fields you pass, and
    returns a before/after diff.

      - To amend a line or two, pass just those fields.
      - To change the destination, pass every field with require_complete=True,
        so a partly supplied address can't merge with the old one (new street,
        old town).

    Check the current address with get_order first. dry_run defaults to True;
    confirm the change with the user before setting it False.

    Args:
        order_id: GUID pkOrderID or numeric order number.
        full_name, company, address1, address2, address3, town, region, postcode,
        country, phone, email: New values. None (default) keeps the current
            value. country is the full name, e.g. "United Kingdom".
        save_to_crm: Also save the address to the customer's CRM record. Default
            False.
        require_complete: Refuse unless full_name, address1, town, postcode and
            country are all passed non-blank. Use it whenever the destination
            changes. Default False.
        dry_run: Default True: shows the change without writing.

    Returns:
        order_id, order_guid, dry_run, status ("dry_run", "updated",
        "no_changes" or "error"), before and after (the changed fields), and
        error when rejected.
    """
    order_id = order_id.strip()

    _FIELD_MAP = {
        "full_name": "FullName",
        "company":   "Company",
        "address1":  "Address1",
        "address2":  "Address2",
        "address3":  "Address3",
        "town":      "Town",
        "region":    "Region",
        "postcode":  "PostCode",
        "country":   "Country",
        "phone":     "PhoneNumber",
        "email":     "EmailAddress",
    }
    user_provided = {
        "full_name": full_name,
        "company":   company,
        "address1":  address1,
        "address2":  address2,
        "address3":  address3,
        "town":      town,
        "region":    region,
        "postcode":  postcode,
        "country":   country,
        "phone":     phone,
        "email":     email,
    }

    if require_complete:
        missing = sorted(
            param for param in ("full_name", "address1", "town", "postcode", "country")
            if not (user_provided[param] or "").strip()
        )
        if missing:
            return {
                "order_id": order_id,
                "status": "error",
                "error": (
                    "require_complete=True but these core address fields are missing "
                    f"or blank: {', '.join(missing)}. Supply the whole destination, "
                    "or set require_complete=False to amend individual lines."
                ),
                "missing_fields": missing,
            }

    raw: dict = {}
    order_guid: str = ""

    if _UUID_RE.match(order_id):
        resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        orders = resp if isinstance(resp, list) else (resp.get("Orders") or resp.get("Data") or [])
        if not orders:
            return {"order_id": order_id, "status": "error", "error": f"No order found for GUID '{order_id}'."}
        raw = orders[0]
        order_guid = raw.get("OrderId") or order_id
    else:
        raw = call_linnworks_get("Orders/GetOrderDetailsByNumOrderId", params={"orderId": order_id})
        if not isinstance(raw, dict) or ("GeneralInfo" not in raw and "NumOrderId" not in raw):
            return {"order_id": order_id, "status": "error", "error": f"No order found for numeric ID '{order_id}'."}
        order_guid = raw.get("OrderId", "")

    if raw.get("Processed"):
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "status": "error",
            "error": (
                "Cannot edit an already-processed order. "
                "Address changes can only be made to open (unprocessed) orders."
            ),
        }

    customer = raw.get("CustomerInfo") or {}
    current_address = customer.get("Address") or {}
    current_billing = customer.get("BillingAddress") or {}
    channel_buyer_name = customer.get("ChannelBuyerName") or ""

    before: dict = {}
    after: dict = {}

    for param, lw_field in _FIELD_MAP.items():
        new_val = user_provided[param]
        if new_val is not None and new_val != current_address.get(lw_field):
            before[param] = current_address.get(lw_field)
            after[param] = new_val

    if not before:
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "The values you provided already match the current address — no update needed.",
        }

    if dry_run:
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply this update.",
            "before": before,
            "after": after,
        }

    new_address = dict(current_address)
    for param, lw_field in _FIELD_MAP.items():
        new_val = user_provided[param]
        if new_val is not None:
            new_address[lw_field] = new_val

    call_linnworks(
        "Orders/SetOrderCustomerInfo",
        {
            "orderId": order_guid,
            "info": {
                "ChannelBuyerName": channel_buyer_name,
                "Address": new_address,
                "BillingAddress": current_billing,
            },
            "saveToCrm": save_to_crm,
        },
    )

    confirmed_resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_guid]})
    confirmed_orders = (
        confirmed_resp if isinstance(confirmed_resp, list)
        else (confirmed_resp.get("Orders") or confirmed_resp.get("Data") or [])
    )

    confirmed_after: dict = {}
    if confirmed_orders:
        confirmed_address = (confirmed_orders[0].get("CustomerInfo") or {}).get("Address") or {}
        for param, lw_field in _FIELD_MAP.items():
            if param in after:
                confirmed_after[param] = confirmed_address.get(lw_field)

    return {
        "order_id": order_id,
        "order_guid": order_guid,
        "dry_run": False,
        "status": "updated",
        "before": before,
        "after": confirmed_after or after,
    }


def _resolve_order_guid(order_id: str) -> tuple[str, dict]:
    order_id = order_id.strip()
    if _UUID_RE.match(order_id):
        resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        orders = resp if isinstance(resp, list) else (resp.get("Orders") or resp.get("Data") or [])
        if not orders:
            raise RuntimeError(f"No order found for GUID '{order_id}'.")
        return orders[0].get("OrderId") or order_id, orders[0]
    else:
        raw = call_linnworks_get(
            "Orders/GetOrderDetailsByNumOrderId",
            params={"orderId": order_id},
        )
        if not isinstance(raw, dict) or ("GeneralInfo" not in raw and "NumOrderId" not in raw):
            raise RuntimeError(f"No order found for numeric ID '{order_id}'.")
        return raw.get("OrderId", ""), raw


@_tool()
def get_order_notes(order_id: str) -> dict:
    """
    Fetch all notes attached to a Linnworks order.

    Works for both open (unprocessed) and processed orders. Returns every
    note with its ID, text, internal flag, creation timestamp, and creator.

    Use this to read the notes on an order before adding, editing, or deleting
    one — the note_id returned here is required by add_order_note,
    update_order_note, and delete_order_note.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.

    Returns:
        A dict with:
          - order_id:    the GUID of the order
          - note_count:  total number of notes
          - notes:       list of note dicts, each with:
              note_id, note, internal, created_on, created_by
    """
    order_id = order_id.strip()

    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})

    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    notes = [_format_order_note(n) for n in raw_notes]

    return {
        "order_id": order_guid,
        "note_count": len(notes),
        "notes": notes,
    }


@_tool()
def add_order_note(
    order_id: str,
    note: str,
    internal: bool = True,
    is_processing_note: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Add a note to a Linnworks order (open or processed).

    The note is added immediately and appears on the order in the Linnworks
    UI. Both open and processed orders accept notes.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the note text is correct — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note: The text of the note to add.
        internal: If True (default), the note is marked as internal (visible
            only to staff, not shown on customer-facing documents).
        is_processing_note: If True, marks the note as a processing note.
            Defaults to False.
        dry_run: If True (default), shows what would be added without writing
            anything. Set to False to actually add the note.

    Returns:
        A dict with:
          - order_id:  the GUID of the order
          - dry_run:   whether this was a dry run
          - success:   True if the note was added (or would be, on dry run)
          - note:      the note text that was (or would be) added
          - internal:  the internal flag value
    """
    order_id = order_id.strip()

    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "note": note,
            "internal": internal,
            "message": "No note written. Set dry_run=False to add this note.",
        }

    call_linnworks(
        "Orders/AddOrdersNote",
        {
            "OrderIds": [order_guid],
            "NoteText": note,
            "IsInternal": internal,
            "IsProcessingNote": is_processing_note,
        },
    )

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    notes_after = [_format_order_note(n) for n in raw_notes]

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "note": note,
        "internal": internal,
        "note_count_after": len(notes_after),
        "notes": notes_after,
    }


@_tool()
def update_order_note(
    order_id: str,
    note_id: str,
    note: str,
    internal: Optional[bool] = None,
    dry_run: bool = True,
) -> dict:
    """
    Edit an existing note on a Linnworks order.

    Linnworks has no dedicated "edit note" endpoint, so this tool uses a
    delete-then-add pattern: the old note is deleted and a new one with the
    updated text is created. The new note gets a new note_id; the old
    note_id will no longer exist after this call.

    Use get_order_notes() first to find the note_id you want to update.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the new text is correct — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note_id: The OrderNoteId GUID of the note to replace (from
            get_order_notes).
        note: The replacement note text.
        internal: Internal flag for the new note. If None (default), the
            existing note's internal flag is preserved.
        dry_run: If True (default), shows the before/after without writing
            anything. Set to False to apply the update.

    Returns:
        A dict with:
          - order_id:     the GUID of the order
          - dry_run:      whether this was a dry run
          - success:      True if the update succeeded (or would, on dry run)
          - old_note_id:  the deleted note's ID
          - old_note:     the text that was replaced
          - new_note:     the replacement text
          - internal:     the internal flag on the new note
    """
    order_id = order_id.strip()
    note_id = note_id.strip()

    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    existing = next(
        (n for n in raw_notes if _note_id_of(n) == note_id),
        None,
    )
    if existing is None:
        return {
            "order_id": order_guid,
            "success": False,
            "error": f"Note '{note_id}' not found on order '{order_guid}'.",
        }

    old_note_text = existing.get("Note") or existing.get("NoteText") or ""
    existing_internal = existing.get("Internal")
    if existing_internal is None:
        existing_internal = existing.get("IsInternal")
    new_internal = internal if internal is not None else bool(existing_internal)

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "old_note_id": note_id,
            "old_note": old_note_text,
            "new_note": note,
            "internal": new_internal,
            "message": (
                "No changes written. Set dry_run=False to delete the old note "
                "and create the new one."
            ),
        }

    call_linnworks(
        "ProcessedOrders/DeleteOrderNote",
        {"pkOrderNoteId": note_id},
    )

    call_linnworks(
        "Orders/AddOrdersNote",
        {
            "OrderIds": [order_guid],
            "NoteText": note,
            "IsInternal": new_internal,
            "IsProcessingNote": False,
        },
    )

    raw_notes_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes_after, list):
        raw_notes_after = (
            raw_notes_after.get("Notes") or []
            if isinstance(raw_notes_after, dict)
            else []
        )

    notes_after = [_format_order_note(n) for n in raw_notes_after]

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "old_note_id": note_id,
        "old_note": old_note_text,
        "new_note": note,
        "internal": new_internal,
        "note_count_after": len(notes_after),
        "notes": notes_after,
    }


@_tool()
def delete_order_note(
    order_id: str,
    note_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Delete a specific note from a Linnworks order.

    Permanently removes the note identified by note_id. This cannot be
    undone — use get_order_notes() to review the note text before deleting.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure you want to delete the note — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note_id: The OrderNoteId GUID of the note to delete (from
            get_order_notes).
        dry_run: If True (default), shows what would be deleted without
            writing anything. Set to False to permanently delete the note.

    Returns:
        A dict with:
          - order_id:         the GUID of the order
          - dry_run:          whether this was a dry run
          - success:          True if the note was deleted (or would be)
          - deleted_note_id:  the ID of the note that was (or would be) deleted
          - deleted_note:     the text of the note that was (or would be) deleted
          - note_count_after: number of notes remaining (live run only)
    """
    order_id = order_id.strip()
    note_id = note_id.strip()

    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    existing = next(
        (n for n in raw_notes if _note_id_of(n) == note_id),
        None,
    )
    if existing is None:
        return {
            "order_id": order_guid,
            "success": False,
            "error": f"Note '{note_id}' not found on order '{order_guid}'.",
        }

    note_text = existing.get("Note") or existing.get("NoteText") or ""

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "deleted_note_id": note_id,
            "deleted_note": note_text,
            "message": "No changes written. Set dry_run=False to permanently delete this note.",
        }

    call_linnworks(
        "ProcessedOrders/DeleteOrderNote",
        {"pkOrderNoteId": note_id},
    )

    raw_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_after, list):
        raw_after = raw_after.get("Notes") or [] if isinstance(raw_after, dict) else []

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "deleted_note_id": note_id,
        "deleted_note": note_text,
        "note_count_after": len(raw_after),
    }


@_tool()
def delete_order_notes_by_text(
    order_id: str,
    text: str,
    match: Literal["exact", "contains", "starts_with"] = "exact",
    case_sensitive: bool = False,
    max_to_delete: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """
    Delete order notes that match a text pattern, without needing to know
    their note_id in advance.

    Fetches all notes on the order, filters by the search text and match mode,
    then deletes each match. Useful for removing test notes, boilerplate, or
    notes identified by their content rather than an opaque GUID.

    Zero matches is not an error — returns success with deleted=0.

    The max_to_delete guard prevents accidental bulk deletion: if more notes
    match than the limit, the tool refuses and lists the matches so you can
    re-scope the search.

    IMPORTANT: dry_run defaults to True. Dry-run output lists every note that
    would be deleted — review it carefully before setting dry_run=False.

    Args:
        order_id: GUID pkOrderID or numeric order number (e.g. "596475").
        text: Text to search for in note content.
        match: How to match the text against each note:
            - "exact" (default): note text must equal search text exactly
            - "contains": note text must contain the search text
            - "starts_with": note text must start with the search text
        case_sensitive: If False (default), comparison is case-insensitive.
        max_to_delete: Safety cap. If more than this many notes match, refuse
            and return the matches so the caller can re-scope. None = no cap.
        dry_run: If True (default), lists what would be deleted without
            deleting anything. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:         whether this was a dry run
          - success:         True (even on zero matches)
          - matched_count:   number of notes that matched
          - deleted:         number actually deleted (0 on dry run)
          - matched:         list of matched notes (note_id, note, created_on, created_by)
          - note_count_after: remaining notes after deletion (live run only)
    """
    order_id = order_id.strip()

    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        try:
            order_guid, _ = _resolve_order_guid(order_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    needle = text if case_sensitive else text.lower()

    def _matches(raw_note: dict) -> bool:
        note_text = raw_note.get("Note") or raw_note.get("NoteText") or ""
        haystack = note_text if case_sensitive else note_text.lower()
        if match == "exact":
            return haystack == needle
        elif match == "contains":
            return needle in haystack
        elif match == "starts_with":
            return haystack.startswith(needle)
        return False

    matched_raw = [n for n in raw_notes if _matches(n)]

    matched_formatted = [_format_order_note(n) for n in matched_raw]

    if max_to_delete is not None and len(matched_raw) > max_to_delete:
        return {
            "success": False,
            "error": (
                f"{len(matched_raw)} notes matched but max_to_delete={max_to_delete}. "
                "Re-scope your search or increase max_to_delete."
            ),
            "matched_count": len(matched_raw),
            "matched": matched_formatted,
        }

    if dry_run:
        return {
            "dry_run": True,
            "success": True,
            "matched_count": len(matched_raw),
            "deleted": 0,
            "matched": matched_formatted,
            "message": (
                "No changes written. Set dry_run=False to delete these notes."
                if matched_raw
                else "No notes matched. Nothing to delete."
            ),
        }

    deleted = 0
    errors: list[str] = []
    for raw_note in matched_raw:
        note_id = _note_id_of(raw_note)
        if not note_id:
            errors.append("Skipped a note with no note_id.")
            continue
        try:
            call_linnworks(
                "ProcessedOrders/DeleteOrderNote",
                {"pkOrderNoteId": note_id},
            )
            deleted += 1
        except Exception as exc:
            errors.append(f"Failed to delete note {note_id}: {exc}")

    raw_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_after, list):
        raw_after = raw_after.get("Notes") or [] if isinstance(raw_after, dict) else []

    result: dict = {
        "dry_run": False,
        "success": deleted == len(matched_raw),
        "matched_count": len(matched_raw),
        "deleted": deleted,
        "matched": matched_formatted,
        "note_count_after": len(raw_after),
    }
    if errors:
        result["errors"] = errors
    return result


@_tool()
def find_open_orders_for_sku(
    sku: str,
    location_id: str = DEFAULT_LOCATION_ID,
) -> dict:
    """
    Find all currently open (unprocessed) orders that contain a given SKU.

    Returns each matching order with customer name, email, order reference,
    dispatch deadline, and the quantity of the requested SKU in that order.

    This is the primary tool for the "PO gap → customer impact" workflow:
    if a purchase order item is delayed or missing, use this to find which
    customers are waiting for that SKU so they can be contacted proactively.

    Searches both top-level order items and composite child components, so
    bundle SKUs are found even when the parent composite is the line item.

    Note: enriching with customer details requires a second API call per
    batch of 50 matched orders. For SKUs with very high open-order volume
    this may be slow — but in practice most SKUs will have a handful of
    open orders at any one time.

    Args:
        sku: The exact SKU to search for (case-insensitive).
        location_id: Linnworks location to query. Defaults to "Default".

    Returns:
        A dict with:
          - sku:            the SKU searched
          - total_open_orders_scanned: total open orders checked
          - match_count:   number of orders containing this SKU
          - orders: list of matching order dicts, each with:
              order_id, num_order_id, reference_num, dispatch_by,
              customer_name, customer_email, quantity (of the matched SKU)
    """
    sku_lower = sku.strip().lower()

    response = call_linnworks(
        "OpenOrders/GetOrdersLowFidelity",
        {"request": {"LocationId": location_id}},
    )
    all_orders = response.get("Orders") or []

    def _find_qty(items: list) -> int:
        total = 0
        for item in items:
            if (item.get("SKU") or "").lower() == sku_lower:
                total += item.get("Quantity") or 0
            children = item.get("CompositeChild") or []
            if children:
                total += _find_qty(children)
        return total

    matched: list[dict] = []
    for order in all_orders:
        qty = _find_qty(order.get("Items") or [])
        if qty > 0:
            matched.append({
                "_guid": order.get("pkOrderID"),
                "order_id": order.get("pkOrderID"),
                "num_order_id": order.get("OrderId"),
                "reference_num": order.get("ReferenceNum"),
                "dispatch_by": order.get("DispatchBy"),
                "quantity": qty,
                "customer_name": "",
                "customer_email": "",
            })

    batch_size = 50
    guids = [o["_guid"] for o in matched if o["_guid"]]
    customer_info: dict[str, dict] = {}

    for i in range(0, len(guids), batch_size):
        batch = guids[i : i + batch_size]
        detail_orders = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        if not isinstance(detail_orders, list):
            detail_orders = detail_orders.get("Orders") or []
        for detail in detail_orders:
            oid = detail.get("OrderId")
            fmt = _format_order_detail(detail)
            customer_info[oid] = {
                "customer_name": fmt.get("customer_name", ""),
                "customer_email": fmt.get("customer_email", ""),
            }

    for order in matched:
        info = customer_info.get(order["_guid"], {})
        order["customer_name"] = info.get("customer_name", "")
        order["customer_email"] = info.get("customer_email", "")
        del order["_guid"]

    return {
        "sku": sku.strip(),
        "total_open_orders_scanned": len(all_orders),
        "match_count": len(matched),
        "orders": matched,
    }


@_tool()
def find_orders_by_reference(
    reference: str,
    include_processed: bool = False,
    location_id: str = DEFAULT_LOCATION_ID,
) -> dict:
    """
    Find Linnworks orders by channel or source reference number.

    Accepts the identifier that appears in customer emails, support tickets,
    and the selling channel's own dashboard — not the internal Linnworks
    numeric order ID. Examples:
      - Shopify:  "11177274" or "#11177274"
      - Amazon:   "202-3420523-7292364"
      - eBay:     the 13-digit reference

    Leading '#' characters are stripped automatically, so you can paste
    references directly from Shopify or customer emails.

    By default only open (unprocessed) orders are searched. Pass
    include_processed=True to also search dispatched orders.

    Returns 0, 1, or multiple matching orders. If more than one order
    matches (e.g. the same reference appears on different channels), all
    are returned so you can disambiguate before acting.

    Backed by OpenOrders/SearchOrders. This endpoint searches across
    ReferenceNum, ExternalReference, and related fields — it is the
    API-native approach for this use case.

    Note: SearchOrders must be sent with an UNWRAPPED body (live-confirmed).
    If it returns an unexpected error, fall back to find_open_orders_for_sku
    or get_open_orders filtered manually.

    Args:
        reference: The channel/source order reference to search for.
            Accepts Shopify "#11177274", Amazon "202-3420523-7292364",
            eBay references, or any Linnworks ReferenceNum/ExternalReference.
        include_processed: If True, also searches dispatched/processed orders.
            Defaults to False (open orders only).
        location_id: Linnworks location to search. Defaults to "Default".

    Returns:
        A dict with:
          - reference:    the reference string searched (# stripped)
          - match_count:  number of orders found
          - orders:       list of matching order dicts, each with:
              order_id, num_order_id, reference_num, external_reference,
              source, sub_source, status, processed, is_parked,
              received_date, customer_name, customer_email

    Note on is_parked vs processed: a CANCELLED order keeps its parked flag
    and its payment status (live-confirmed 18 Sep 2026 — a cancelled parked
    order reads processed=True alongside is_parked=True, status=0). So
    `processed` is the only reliable "is this order finished" signal; treat
    is_parked as "is it held out of the dispatch queue", nothing more.
    """
    reference = reference.strip().lstrip("#").strip()
    if not reference:
        return {"error": "reference must not be empty after stripping '#'"}

    resp = call_linnworks(
        "OpenOrders/SearchOrders",
        {
            "LocationId": location_id,
            "SearchTerm": reference,
            "IncludeProcessed": include_processed,
        },
    )

    seen: set[str] = set()
    all_guids: list[str] = []

    for view in (resp.get("OpenOrders") or []):
        for guid in (view.get("OrderIds") or []):
            if guid and guid not in seen:
                seen.add(guid)
                all_guids.append(guid)

    if include_processed:
        for guid in (resp.get("ProcessedOrders") or []):
            if guid and guid not in seen:
                seen.add(guid)
                all_guids.append(guid)

    if not all_guids:
        return {
            "reference": reference,
            "match_count": 0,
            "orders": [],
        }

    orders: list[dict] = []
    batch_size = 50
    for i in range(0, len(all_guids), batch_size):
        batch = all_guids[i : i + batch_size]
        detail_resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        detail_list = (
            detail_resp if isinstance(detail_resp, list)
            else (detail_resp.get("Orders") or detail_resp.get("Data") or [])
        )
        for order in detail_list:
            fmt = _format_order_detail(order)
            orders.append({
                "order_id":           fmt["order_id"],
                "num_order_id":       fmt["num_order_id"],
                "reference_num":      fmt["reference_num"],
                "external_reference": fmt["external_reference"],
                "source":             fmt["source"],
                "sub_source":         fmt["sub_source"],
                "status":             fmt["status"],
                "processed":          fmt["processed"],
                "is_parked":          fmt["is_parked"],
                "received_date":      fmt["received_date"],
                "customer_name":      fmt["customer_name"],
                "customer_email":     fmt["customer_email"],
            })

    return {
        "reference": reference,
        "match_count": len(orders),
        "orders": orders,
    }


REFUND_PUSH_CHANNELS: dict[str, dict] = {
    "SHOPIFY": {
        "state": "proven",
        "evidence": "live 2026-09-23, a test order: "
                    "ActionRefund returned PROCESSED and the refund "
                    "appeared on Shopify as a refund for the full "
                    "GBP 1.90, both lines, with the Linnworks note propagated "
                    "verbatim -- and the Linnworks refund header rewrote its "
                    "ExternalReference to that same Shopify refund id, which "
                    "ties the two records together",
    },
    "AMAZON": {"state": "never_attempted", "evidence": None},
    "EBAY": {"state": "never_attempted", "evidence": None},
}

REFUND_CHANNEL_PUSH_PROVEN = all(
    c["state"] == "proven" for c in REFUND_PUSH_CHANNELS.values())

REFUND_CHANNEL_PUSH_WARNING = (
    "⚠️ Pushing this refund to the sales channel (ReturnsRefunds/ActionRefund) "
    "is PROVEN to reach SHOPIFY (live 2026-09-23, a test order: the refund "
    "appeared on the storefront for the full amount, both lines, and the "
    "Linnworks header carries the Shopify refund id). It has NEVER been tested "
    "on AMAZON or EBAY, and do not assume it carries over: the two previous "
    "channel-push investigations in this account both ended with Linnworks "
    "accepting a push that never reached the channel. On any channel other "
    "than Shopify, Linnworks accepting the call (a 2xx, or even "
    "SuccessfullyActioned=True) is NOT evidence the customer has been paid. "
    "Check that channel's own admin, not Linnworks. Proven on Shopify only for "
    "a FULL refund via refund_order; a partial refund via refund_order_lines, "
    "and the postage component of any refund, are still untested."
)


def _refund_push_state(order_source: Optional[str]) -> str:
    key = (order_source or "").strip().upper()
    entry = REFUND_PUSH_CHANNELS.get(key)
    return entry["state"] if entry else "never_attempted"


def _refund_channel_push_warning(order_source: Optional[str] = None) -> Optional[str]:
    if _refund_push_state(order_source) == "proven":
        return None
    return REFUND_CHANNEL_PUSH_WARNING


_REFUND_UNCONFIRMED_MESSAGE = (
    "The refund could not be confirmed by re-reading the order's refund "
    "headers, but this does NOT mean the refund failed or was not created -- "
    "CreateRefund itself succeeded. A read-back can race the write. "
    "DO NOT re-run this refund on this order -- running it again risks "
    "refunding the customer twice. Check the order directly in Linnworks "
    "before doing anything else."
)


def _get_refund_headers(order_guid: str) -> list:
    resp = call_linnworks(
        "ReturnsRefunds/GetRefundHeadersByOrderId",
        {"request": {"OrderId": order_guid}},
    )
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("RefundHeaders") or resp.get("Data") or []
    return []


def _apply_refund_readback(result: dict, order_guid: str, refund_header_id) -> dict:
    if refund_header_id is None:
        result["outcome"] = "read_back_failed"
        result["read_back_error"] = (
            "CreateRefund did not return a RefundHeaderId to match against."
        )
        return result

    try:
        headers = _get_refund_headers(order_guid)
    except RateLimitError as exc:
        result["outcome"] = "rate_limited"
        result["read_back_error"] = f"rate_limited reading refund headers: {exc}"
        return result
    except RuntimeError as exc:
        result["outcome"] = "read_back_failed"
        result["read_back_error"] = f"reading refund headers failed: {exc}"
        return result

    match = next(
        (h for h in headers if h.get("RefundHeaderId") == refund_header_id), None
    )
    result["refund_headers_found"] = len(headers)
    if match is not None:
        result["outcome"] = "confirmed"
        result["matched_refund_header"] = match
    else:
        result["outcome"] = "unconfirmed"
        result["unconfirmed_message"] = _REFUND_UNCONFIRMED_MESSAGE
    return result


def _check_refund_eligibility(options: dict, push_to_channel: bool) -> str | None:
    cannot_reason = options.get("CannotRefundReason") or "None"
    if cannot_reason != "None":
        return f"Linnworks cannot refund this order: {cannot_reason}"

    if push_to_channel and options.get("CanRefund") is False:
        internally = options.get("CanRefundInternally")
        hint = (
            " Linnworks reports it CAN be refunded internally, so retry with "
            "push_to_channel=False to book the refund without pushing it to a "
            "channel."
            if internally else ""
        )
        return (
            "Linnworks reports CanRefund=False for this order, so a refund cannot "
            "be pushed to the channel. This is normal for a manually created "
            "(DIRECT) order, which has no channel to push to." + hint
        )

    if not push_to_channel and options.get("CanRefundInternally") is False:
        return (
            "Linnworks reports CanRefundInternally=False for this order, so no "
            "refund can be booked against it."
        )

    return None


def _get_refund_options(order_guid: str) -> dict:
    return call_linnworks(
        "ReturnsRefunds/GetRefundOptions",
        {"request": {"OrderId": order_guid}},
    )


@_tool()
def cancel_order(
    order_id: str,
    note: Optional[str] = None,
    refund: float = 0.0,
    dry_run: bool = True,
) -> dict:
    """
    Cancel an open (unprocessed) order with Orders/CancelOrder. A processed
    (despatched) order is refused. Cancellations can't be undone through the API:
    dry_run defaults to True, and you should confirm with the user before setting
    it False.

    There is no return-to-stock option; that follows your Linnworks workspace
    settings.

    After a live cancellation the order is re-read by its GUID (a cancelled order
    drops out of open-order searches, so a number lookup would fail). `outcome` is
    "cancelled" (it now reads processed), "not_cancelled", "unconfirmed" (the
    read-back failed; never reported as a failed cancel) or "rate_limited". Only
    `processed` decides it: a cancelled order can still read as parked and keep
    its payment status, so those are shown for information only.

    Args:
        order_id: GUID pkOrderID or numeric order number.
        note: Optional note for the cancellation.
        refund: Refund amount to attach. Default 0.0 (no refund). Always sent,
            because Linnworks rejects a cancel without it.
        dry_run: Default True: shows what would be cancelled.

    Returns:
        dry_run, order_id (the GUID), num_order_id, customer_name, items, status
        ("would_cancel" or "cancelled"); and on a live run: outcome,
        read_back_is_parked and read_back_status.
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if fmt.get("processed"):
        return {
            "error": (
                "This order is already processed (dispatched). "
                "Only open orders can be cancelled via this tool."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    summary = {
        "order_id":           fmt.get("order_id"),
        "num_order_id":       fmt.get("num_order_id"),
        "customer_name":      fmt.get("customer_name"),
        "customer_email":     fmt.get("customer_email"),
        "reference_num":      fmt.get("reference_num"),
        "external_reference": fmt.get("external_reference"),
        "source":             fmt.get("source"),
        "items":              fmt.get("items", []),
        "note":               note,
    }

    if dry_run:
        return {
            "dry_run": True,
            "status": "would_cancel",
            "message": "Set dry_run=False to execute this cancellation.",
            **summary,
        }

    fulfilment_location_id = (
        raw.get("FulfilmentLocationId") or DEFAULT_LOCATION_ID
    )
    payload = {
        "orderId": guid,
        "fulfilmentCenter": fulfilment_location_id,
        "refund": float(refund),
        "note": note or "",
    }
    write_result = call_linnworks("Orders/CancelOrder", payload)

    result = {
        "dry_run": False,
        "status": "cancelled",
        "linnworks_response": write_result,
        **summary,
    }

    try:
        _, raw_after = _resolve_order_guid(guid)
    except RateLimitError as exc:
        result["outcome"] = "rate_limited"
        result["read_back_error"] = f"rate_limited on read-back: {exc}"
        return result
    except RuntimeError as exc:
        result["outcome"] = "unconfirmed"
        result["read_back_error"] = f"read-back failed: {exc}"
        return result

    fmt_after = _format_order_detail(raw_after)
    result["outcome"] = "cancelled" if fmt_after.get("processed") else "not_cancelled"
    result["read_back_processed"] = fmt_after.get("processed")
    result["read_back_is_parked"] = fmt_after.get("is_parked")
    result["read_back_status"] = fmt_after.get("status")
    return result


_REMOVE_ORDER_ITEM_UNPROVEN_WARNING = (
    "⚠️ Orders/RemoveOrderItem is UNPROVEN in this account — it has only ever "
    "been probed with a deliberately invalid payload; this build "
    "never fired it against a real order. A 2xx response here is NOT proof "
    "the line was removed. Verify this order in the Linnworks UI, and "
    "re-run find_unlinked_order_lines on this SAME order to confirm the "
    "lines you did NOT touch still have a healthy channel-side reference — "
    "a related order-item write has been observed to orphan surviving "
    "lines."
)


@_tool()
def remove_order_item(
    order_id: str,
    row_id: str,
    location_id: Optional[str] = None,
    allow_empty_order: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Remove one line from an OPEN (unprocessed) order. A processed or despatched
    order is refused.

    ⚠️ NEVER FIRED BY THIS TOOL. Every live response carries an unproven warning:
    check the order in the Linnworks UI afterwards, and re-run
    find_unlinked_order_lines on the SAME order, because a related order-item
    write has been seen to orphan the channel link on lines that were not touched.
    There is no known way to undo a removal through the API, so review the
    manifest with the user before setting dry_run=False.

    Refused before any write: an unknown row_id (get row ids from get_order), and
    removing the LAST line on an order unless allow_empty_order=True (what a
    zero-line order does downstream is unknown). If the line is a composite
    parent, the manifest lists its children under
    removed_line["composite_children"]; removing the parent removes them too.

    After a live write the order is re-read and the outcome is "removed",
    "still_present" (accepted but the line is still there), "unconfirmed" (the
    read-back failed; never reported as success or "not found") or
    "rate_limited". order_total_before and order_total_after are always shown,
    because whether Linnworks recalculates the total is not guaranteed.

    Args:
        order_id: GUID pkOrderID or numeric order number.
        row_id: The line's row_id from get_order.
        location_id: fulfilmentCenter to send. Defaults to the order's own
            fulfilment location, then the Default location.
        allow_empty_order: Must be True to remove an order's last line.
            Default False.
        dry_run: Default True: shows what would be removed, writes nothing.

    Returns:
        On a refusal, an "error" key and nothing written. Otherwise: dry_run,
        order_id, num_order_id, customer_name, reference_num, external_reference,
        row_id, removed_line (sku, title, quantity, price_per_unit, line_total,
        composite_children), remaining_line_count_before, order_total_before,
        currency; and after a live write: outcome, remaining_line_count,
        order_total_after, linnworks_response, unproven_warning.
    """
    order_id = order_id.strip()
    row_id = row_id.strip()

    try:
        guid, raw = _resolve_order_guid(order_id)
    except RateLimitError as exc:
        return {
            "outcome": "rate_limited",
            "error": f"rate_limited resolving order: {exc}",
            "order_id_input": order_id,
        }
    except RuntimeError as exc:
        return {"error": str(exc), "order_id_input": order_id}

    fmt = _format_order_detail(raw)

    if fmt.get("processed"):
        return {
            "error": (
                "This order is already processed (dispatched). "
                "remove_order_item only operates on open orders."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    raw_items = list(raw.get("Items") or [])
    target = next((i for i in raw_items if i.get("RowId") == row_id), None)

    if target is None:
        return {
            "error": (
                f"row_id {row_id!r} not found on order {fmt.get('order_id')!r}. "
                "Use get_order() to list current line items and their row_id values."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "row_id": row_id,
        }

    if len(raw_items) <= 1 and not allow_empty_order:
        return {
            "error": (
                "This is the last remaining line on the order. Removing it "
                "would leave a zero-line order, and its behaviour in "
                "Linnworks (dispatchable or not) is unknown. Set "
                "allow_empty_order=True to proceed anyway."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "row_id": row_id,
        }

    removed_line = {
        "sku": target.get("SKU"),
        "title": target.get("Title"),
        "quantity": target.get("Quantity"),
        "price_per_unit": target.get("PricePerUnit"),
        "line_total": target.get("CostIncTax"),
    }
    composite_children = [
        {"sku": c.get("SKU"), "quantity": c.get("Quantity")}
        for c in (target.get("CompositeSubItems") or [])
    ]
    if composite_children:
        removed_line["composite_children"] = composite_children

    totals_raw = raw.get("TotalsInfo") or {}

    manifest = {
        "order_id": fmt.get("order_id"),
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "reference_num": fmt.get("reference_num"),
        "external_reference": fmt.get("external_reference"),
        "row_id": row_id,
        "removed_line": removed_line,
        "remaining_line_count_before": len(raw_items),
        "order_total_before": totals_raw.get("TotalCharge"),
        "currency": totals_raw.get("Currency"),
    }

    if dry_run:
        return {
            "dry_run": True,
            "status": "would_remove",
            "message": (
                "Set dry_run=False to remove this line. Set "
                "allow_empty_order=True if this is the order's last line."
            ),
            **manifest,
        }

    fulfilment_centre = (
        location_id or raw.get("FulfilmentLocationId") or DEFAULT_LOCATION_ID
    )
    payload = {
        "orderId": guid,
        "rowid": row_id,
        "fulfilmentCenter": fulfilment_centre,
    }
    try:
        write_resp = call_linnworks("Orders/RemoveOrderItem", payload)
    except RateLimitError as exc:
        return {
            "dry_run": False,
            "outcome": "rate_limited",
            "error": f"rate_limited on Orders/RemoveOrderItem: {exc}",
            "order_total_after": None,
            **manifest,
            "unproven_warning": _REMOVE_ORDER_ITEM_UNPROVEN_WARNING,
        }

    try:
        _, raw_after = _resolve_order_guid(guid)
    except RateLimitError as exc:
        return {
            "dry_run": False,
            "outcome": "unconfirmed",
            "unconfirmed_reason": f"rate_limited on read-back: {exc}",
            "linnworks_response": write_resp,
            "order_total_after": None,
            **manifest,
            "unproven_warning": _REMOVE_ORDER_ITEM_UNPROVEN_WARNING,
        }
    except RuntimeError as exc:
        return {
            "dry_run": False,
            "outcome": "unconfirmed",
            "unconfirmed_reason": f"read-back failed: {exc}",
            "linnworks_response": write_resp,
            "order_total_after": None,
            **manifest,
            "unproven_warning": _REMOVE_ORDER_ITEM_UNPROVEN_WARNING,
        }

    items_after = list(raw_after.get("Items") or [])
    still_present = any(i.get("RowId") == row_id for i in items_after)
    totals_after = raw_after.get("TotalsInfo") or {}
    outcome = "still_present" if still_present else "removed"

    result = {
        "dry_run": False,
        "outcome": outcome,
        "linnworks_response": write_resp,
        **manifest,
        "remaining_line_count": len(items_after),
        "order_total_after": totals_after.get("TotalCharge"),
        "unproven_warning": _REMOVE_ORDER_ITEM_UNPROVEN_WARNING,
    }
    if outcome == "still_present":
        result["warning"] = (
            "The row_id is still present on a fresh read-back — Linnworks "
            "accepted the call but did NOT remove the line. Check the order "
            "in the Linnworks UI before trying again."
        )
    return result


UPDATE_ITEM_PROVEN = "proven"
UPDATE_ITEM_NEVER_ATTEMPTED = "never_attempted"
UPDATE_ITEM_PROVEN_NON_EMPTY_ONLY = "proven_non_empty_only"
UPDATE_ITEM_OBSERVED_STATES = (UPDATE_ITEM_PROVEN, UPDATE_ITEM_NEVER_ATTEMPTED,
                               UPDATE_ITEM_PROVEN_NON_EMPTY_ONLY)

UPDATE_ITEM_PROVEN_HERE = "this_repo"
UPDATE_ITEM_PROVEN_ELSEWHERE = "other_repo"
UPDATE_ITEM_PROVENANCES = (UPDATE_ITEM_PROVEN_HERE, UPDATE_ITEM_PROVEN_ELSEWHERE)

UPDATE_ORDER_ITEM_FIELDS: dict[str, dict] = {
    "PricePerUnit": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-09, a separate order-sync process",
    },
    "TaxRate": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-09, a separate order-sync process",
    },
    "Quantity": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-15 on a test order, a separate order-sync process",
    },
    "ItemNumber": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_HERE,
        "evidence": "live 2026-09-18 on a test order; restored "
                    "byte-identically across all 49 raw line fields, and the "
                    "written value was still present on a re-read 5 days later",
    },
    "ItemSource": {
        "state": UPDATE_ITEM_PROVEN_NON_EMPTY_ONLY,
        "proven_by": UPDATE_ITEM_PROVEN_HERE,
        "evidence": "live 2026-09-18 on a test order: writing a "
                    "DIFFERENT non-empty value ('EBAY') persisted, and so did "
                    "restoring 'SHOPIFY', but writing '' was SILENTLY DISCARDED "
                    "behind a 200 and read back unchanged. A repair writes "
                    "blank->non-blank, which is the proven direction. A "
                    "consequence worth keeping: because the API will not accept "
                    "an empty value, no API caller can blank a line's channel "
                    "link, so an orphan cannot be manufactured for testing and "
                    "cannot have been caused by this server",
    },
}

UPDATE_ORDER_ITEM_PROOF_SHAPE: dict = {
    "fulfilment_center_selection": "derived_from_order",
    "source_selection": "derived_from_order_source",
    "fulfilment_center_value_on_proving_orders": DEFAULT_LOCATION_ID,
    "proving_orders_location_name": "Default",
    "proving_orders": ("test-order", "test-order", "test-order"),
    "caveat": (
        "the selection mechanism behind a test order's write is not "
        "established here -- only the value it sent (the zero GUID) is"
    ),
}

FULFILMENT_CENTER_EFFECT_OBSERVED = UPDATE_ITEM_NEVER_ATTEMPTED
FULFILMENT_CENTER_EFFECT_EVIDENCE = ""

DESPATCH_MAPPING_OBSERVED = UPDATE_ITEM_PROVEN

DESPATCH_MAPPING_WARNING_TEXT = (
    "a despatch after an API write DOES reach the storefront and mark the "
    "correct line fulfilled, and ItemNumber is the field that steers it (live "
    "2026-09-23, a test order). ChannelSKU is excluded: a line whose ItemNumber "
    "pointed elsewhere did NOT fulfil its own SKU's storefront line. So writing "
    "ItemNumber is what makes a despatch land, which is what this tool writes. "
    "Two limits remain. A line whose ItemNumber points at the WRONG storefront "
    "line will fulfil that wrong line, silently and successfully -- so a wrong "
    "id is worse than a blank one, which is why channel_line_id is never "
    "guessed here. And NO TOOL IN THIS SERVER CAN DESPATCH AN ORDER, so none of "
    "this can be re-verified from here: update_pick_wave refuses "
    "Complete/Packing/Shipped by design"
)

DESPATCH_MAPPING_EVIDENCE = (
    "live 2026-09-23 on a test order, with "
    "a separate order-sync process SUSPENDED so nothing could race the run. A line's "
    "ItemNumber was repointed at a sibling line's storefront id, its ChannelSKU "
    "left untouched, so nothing pointed at the sibling's own storefront line. "
    "On despatch Shopify went PARTIALLY_FULFILLED: it fulfilled ONLY the line "
    "named by ItemNumber and left the other unfulfilled, despite a despatched "
    "Linnworks line carrying that line's exact ChannelSKU. So a despatch does "
    "reach the storefront after an API write, AND ItemNumber is what steers it. "
    "Corroborated the same day from a second subsystem: a refund push on the "
    "same order failed with 'the requested refund amount exceeds the amount "
    "available to refund for this line' on BOTH lines, because both resolved to "
    "the SAME storefront line -- the refunds adapter resolves by ItemNumber too, "
    "so this is not a quirk of the fulfilment path. Also observed: an over-claim "
    "does NOT break the push (two Linnworks units claimed a one-unit storefront "
    "line; Shopify fulfilled one and dropped the excess), which is why "
    "the raced run came back non-discriminating rather than erroring. NOTE no "
    "tool in this server can despatch an order: update_pick_wave refuses "
    "Complete/Packing/Shipped by design, so any repeat needs a Linnworks UI "
    "despatch, and any other process that edits "
    "the order suspended"
)


DESPATCH_MAPPING_KEY = "ItemNumber"
DESPATCH_MAPPING_KEY_CANDIDATES = ("ItemNumber", "ChannelSKU")


def _update_order_item_proven_fields() -> str:
    return ", ".join(
        name for name, entry in UPDATE_ORDER_ITEM_FIELDS.items()
        if entry["state"] != UPDATE_ITEM_NEVER_ATTEMPTED
        and entry["proven_by"] == UPDATE_ITEM_PROVEN_HERE
    )


def _assert_update_order_item_observations_consistent() -> None:
    for field, entry in UPDATE_ORDER_ITEM_FIELDS.items():
        state = entry.get("state")
        if state not in UPDATE_ITEM_OBSERVED_STATES:
            raise ValueError(
                f"UPDATE_ORDER_ITEM_FIELDS['{field}'].state={state!r} is not one "
                f"of {UPDATE_ITEM_OBSERVED_STATES}"
            )
        if state != UPDATE_ITEM_NEVER_ATTEMPTED and not entry.get("evidence"):
            raise ValueError(
                f"UPDATE_ORDER_ITEM_FIELDS['{field}'] is {state} but carries no "
                "evidence; the warning text is interpolated from it"
            )
        if state != UPDATE_ITEM_NEVER_ATTEMPTED and entry.get(
                "proven_by") not in UPDATE_ITEM_PROVENANCES:
            raise ValueError(
                f"UPDATE_ORDER_ITEM_FIELDS['{field}'] is {state} but its "
                f"proven_by={entry.get('proven_by')!r} is not one of "
                f"{UPDATE_ITEM_PROVENANCES}; the runtime warning may only name "
                "fields proven in THIS repo"
            )
    if DESPATCH_MAPPING_OBSERVED not in UPDATE_ITEM_OBSERVED_STATES:
        raise ValueError(
            f"DESPATCH_MAPPING_OBSERVED={DESPATCH_MAPPING_OBSERVED!r} is not one "
            f"of {UPDATE_ITEM_OBSERVED_STATES}"
        )
    if not DESPATCH_MAPPING_EVIDENCE:
        raise ValueError("DESPATCH_MAPPING_OBSERVED carries no evidence text")
    if (UPDATE_ORDER_ITEM_PROOF_SHAPE["fulfilment_center_value_on_proving_orders"]
            != DEFAULT_LOCATION_ID):
        raise ValueError(
            "UPDATE_ORDER_ITEM_PROOF_SHAPE claims the proving orders' "
            "fulfilmentCenter was not DEFAULT_LOCATION_ID -- the 'proof "
            "covers Default-location orders only' wording relies on this "
            "being true; update the wording before changing this value"
        )
    if FULFILMENT_CENTER_EFFECT_OBSERVED not in UPDATE_ITEM_OBSERVED_STATES:
        raise ValueError(
            "FULFILMENT_CENTER_EFFECT_OBSERVED="
            f"{FULFILMENT_CENTER_EFFECT_OBSERVED!r} is not one of "
            f"{UPDATE_ITEM_OBSERVED_STATES}"
        )
    if (FULFILMENT_CENTER_EFFECT_OBSERVED != UPDATE_ITEM_NEVER_ATTEMPTED
            and not FULFILMENT_CENTER_EFFECT_EVIDENCE):
        raise ValueError(
            "FULFILMENT_CENTER_EFFECT_OBSERVED is "
            f"{FULFILMENT_CENTER_EFFECT_OBSERVED} but carries no evidence; "
            "the warning text is interpolated from it"
        )
    if (DESPATCH_MAPPING_OBSERVED == UPDATE_ITEM_PROVEN
            and UPDATE_ORDER_ITEM_FIELDS["ItemNumber"]["state"]
            == UPDATE_ITEM_NEVER_ATTEMPTED):
        raise ValueError(
            "DESPATCH_MAPPING_OBSERVED claims proven while ItemNumber was never "
            "written: despatch mapping cannot be proven without the write"
        )
    if DESPATCH_MAPPING_KEY is not None:
        if DESPATCH_MAPPING_KEY not in DESPATCH_MAPPING_KEY_CANDIDATES:
            raise ValueError(
                f"DESPATCH_MAPPING_KEY={DESPATCH_MAPPING_KEY!r} is not one of "
                f"{DESPATCH_MAPPING_KEY_CANDIDATES}"
            )
        if DESPATCH_MAPPING_OBSERVED != UPDATE_ITEM_PROVEN:
            raise ValueError(
                f"DESPATCH_MAPPING_KEY={DESPATCH_MAPPING_KEY!r} names the field "
                "despatch matches on, but DESPATCH_MAPPING_OBSERVED="
                f"{DESPATCH_MAPPING_OBSERVED!r}: the key cannot be known while "
                "the mapping itself is not"
            )


_assert_update_order_item_observations_consistent()


REMOVE_ORDER_ITEM_OBSERVATIONS: dict[str, dict] = {
    "endpoint_removes_a_line": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-23 07:16:30 on a test order, a separate order-sync process: "
                    "the removed line was gone on a direct re-read, and "
                    "the two untouched lines kept their channel reference",
    },
    "lowercase_rowid_key": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-23 on a test order, a separate order-sync process: the "
                    "payload sent the documented-but-unverified lowercase "
                    "'rowid' key and the removal took effect",
    },
    "order_total_recalculated": {
        "state": UPDATE_ITEM_PROVEN, "proven_by": UPDATE_ITEM_PROVEN_ELSEWHERE,
        "evidence": "live 2026-09-23 on a test order, a separate order-sync process: "
                    "TotalCharge went 82.30 -> 75.85, matching Shopify's "
                    "currentTotalPriceSet. ONE observation, on ONE Shopify "
                    "order, after a Shopify order edit -- not a general rule, "
                    "which is why order_total_before/after is still reported",
    },
    "non_default_fulfilment_center": {
        "state": UPDATE_ITEM_NEVER_ATTEMPTED, "proven_by": None,
        "evidence": "",
        "why_not": "a test order sits at the Default location, which on this "
                   "tenant IS the zero GUID -- so the hard-coded ZERO_GUID "
                   "a separate order-sync process sends and the FulfilmentLocationId this "
                   "tool derives were identical bytes. The run says nothing "
                   "about a non-Default order.",
    },
    "removing_the_last_line": {
        "state": UPDATE_ITEM_NEVER_ATTEMPTED, "proven_by": None,
        "evidence": "",
        "why_not": "the test order had three lines and kept two. What Linnworks does "
                   "to an order emptied of its last line is untested, which is "
                   "why remove_order_item still refuses it without "
                   "allow_empty_order=True.",
    },
    "channel_sync_re_adds_a_removed_line": {
        "state": UPDATE_ITEM_NEVER_ATTEMPTED, "proven_by": None,
        "evidence": "",
        "why_not": "the checklist asked for a re-read hours later; the "
                   "23 Sep confirmation was immediate. Whether a channel sync "
                   "silently puts the line back is unknown.",
    },
}

_REMOVE_ORDER_ITEM_REQUIRED_OBSERVATIONS = (
    "endpoint_removes_a_line",
    "lowercase_rowid_key",
    "order_total_recalculated",
    "non_default_fulfilment_center",
    "removing_the_last_line",
    "channel_sync_re_adds_a_removed_line",
)

_OTHER_REPO_MARKERS = ("a separate order-sync process", "test-order")

_REMOVE_ORDER_ITEM_NEVER_FIRED_HERE = "never fired it against a real order"


def _assert_remove_order_item_observations_consistent() -> None:
    for name in _REMOVE_ORDER_ITEM_REQUIRED_OBSERVATIONS:
        if name not in REMOVE_ORDER_ITEM_OBSERVATIONS:
            raise ValueError(
                f"REMOVE_ORDER_ITEM_OBSERVATIONS is missing '{name}'; an "
                "unknown that is absent reads as one nobody thought of"
            )
    for name, entry in REMOVE_ORDER_ITEM_OBSERVATIONS.items():
        state = entry.get("state")
        if state not in UPDATE_ITEM_OBSERVED_STATES:
            raise ValueError(
                f"REMOVE_ORDER_ITEM_OBSERVATIONS['{name}'].state={state!r} is "
                f"not one of {UPDATE_ITEM_OBSERVED_STATES}"
            )
        if state == UPDATE_ITEM_NEVER_ATTEMPTED:
            if entry.get("proven_by") is not None:
                raise ValueError(
                    f"REMOVE_ORDER_ITEM_OBSERVATIONS['{name}'] is "
                    f"never_attempted but names proven_by="
                    f"{entry.get('proven_by')!r}; a provenance on an "
                    "unattempted observation reads as proof"
                )
            continue
        if not entry.get("evidence"):
            raise ValueError(
                f"REMOVE_ORDER_ITEM_OBSERVATIONS['{name}'] is {state} but "
                "carries no evidence; a proof nobody can check is not one"
            )
        if entry.get("proven_by") not in UPDATE_ITEM_PROVENANCES:
            raise ValueError(
                f"REMOVE_ORDER_ITEM_OBSERVATIONS['{name}'] is {state} but its "
                f"proven_by={entry.get('proven_by')!r} is not one of "
                f"{UPDATE_ITEM_PROVENANCES}"
            )
    proven_here = any(
        entry.get("proven_by") == UPDATE_ITEM_PROVEN_HERE
        for entry in REMOVE_ORDER_ITEM_OBSERVATIONS.values()
    )
    if (not proven_here
            and _REMOVE_ORDER_ITEM_NEVER_FIRED_HERE
            not in _REMOVE_ORDER_ITEM_UNPROVEN_WARNING):
        raise ValueError(
            "_REMOVE_ORDER_ITEM_UNPROVEN_WARNING no longer says this build "
            f"{_REMOVE_ORDER_ITEM_NEVER_FIRED_HERE!r}, but no "
            "REMOVE_ORDER_ITEM_OBSERVATIONS row is proven_by this repo. "
            "Another repository's run does not soften this build's warning"
            ""
        )
    if (proven_here
            and _REMOVE_ORDER_ITEM_NEVER_FIRED_HERE
            in _REMOVE_ORDER_ITEM_UNPROVEN_WARNING):
        raise ValueError(
            "a REMOVE_ORDER_ITEM_OBSERVATIONS row is proven_by this repo, but "
            "_REMOVE_ORDER_ITEM_UNPROVEN_WARNING still says this build "
            f"{_REMOVE_ORDER_ITEM_NEVER_FIRED_HERE!r}"
        )
    for marker in _OTHER_REPO_MARKERS:
        if marker in _REMOVE_ORDER_ITEM_UNPROVEN_WARNING:
            raise ValueError(
                f"_REMOVE_ORDER_ITEM_UNPROVEN_WARNING names {marker!r}: "
                "another repository's evidence must not reach runtime text"
                ". It belongs in "
                "REMOVE_ORDER_ITEM_OBSERVATIONS"
            )


_assert_remove_order_item_observations_consistent()


def _build_relink_order_line_warning() -> str:
    return (
        "⚠️ Orders/UpdateOrderItem writes are PROVEN in this account, by this repo, "
        f"to persist for: {_update_order_item_proven_fields()} — see "
        "UPDATE_ORDER_ITEM_FIELDS for the evidence per field. Note ItemSource "
        "persists only for a NON-EMPTY "
        "value: writing an empty string is silently discarded behind a 200, so a "
        "2xx is still NOT proof on its own. This tool re-reads the order after "
        "the write, so check the outcome is 'relinked', not 'not_persisted' or "
        "'unconfirmed', and check unexpected_field_changes is empty — this "
        "endpoint takes the whole line, so price, quantity and tax ride on the "
        "same object. "
        f"SEPARATELY ({DESPATCH_MAPPING_OBSERVED}, with a caveat): whether despatch "
        "then maps this line back to its storefront line. Persisting the fields "
        "and despatch honouring them are different claims — "
        f"{DESPATCH_MAPPING_WARNING_TEXT}. "
        "What IS established is that a line with NO channel link does not fulfil "
        "on the storefront (22 orders, 1 Aug-19 Sep 2026), and that matching is "
        "not done by the Linnworks SKU. Whether the key is ItemNumber or "
        "ChannelSKU is not yet known. So: re-run find_unlinked_order_lines on this "
        "SAME order to confirm the line reads as linked — bearing in mind that "
        "check only sees a MISSING link, never a link pointing at the WRONG "
        "storefront line — and after despatch check the storefront order shows "
        "this line, and only this line, fulfilled. "
        "⚠️ PAYLOAD SHAPE AND LOCATION SCOPE: this tool derives "
        f"fulfilmentCenter {UPDATE_ORDER_ITEM_PROOF_SHAPE['fulfilment_center_selection']} "
        f"and source/subSource {UPDATE_ORDER_ITEM_PROOF_SHAPE['source_selection']}. "
        "On every proving order "
        f"({', '.join(UPDATE_ORDER_ITEM_PROOF_SHAPE['proving_orders'])}) the "
        "fulfilmentCenter VALUE that reached Linnworks was the "
        f"{UPDATE_ORDER_ITEM_PROOF_SHAPE['proving_orders_location_name']}/zero GUID — "
        "at that location this tool's derivation and a caller that simply "
        "hard-codes the zero GUID produce IDENTICAL bytes, so THE PROOF COVERS "
        "DEFAULT-LOCATION ORDERS ONLY and cannot tell the two apart there. "
        "Whether fulfilmentCenter's VALUE has any effect on this endpoint at "
        f"all is {FULFILMENT_CENTER_EFFECT_OBSERVED} — nothing has ever sent a "
        "fulfilmentCenter that disagreed with the order's real location, so a "
        "caller that hard-codes a fixed value regardless of the order's real "
        "location is NOT covered by this proof at a non-Default location. The "
        "dry-run manifest shows the fulfilmentCenter this call will send and "
        "flags it when the order sits away from Default."
    )


_RELINK_ORDER_LINE_UNPROVEN_WARNING = _build_relink_order_line_warning()


_RELINK_MISSING = object()


def _build_relink_order_item(raw_item: dict, channel_line_id: str, channel_line_source: str) -> dict:
    payload = dict(raw_item)
    payload["ItemNumber"] = channel_line_id
    payload["ItemSource"] = channel_line_source
    return payload


def _relink_payload_unexpected_diff(original: dict, payload: dict) -> dict:
    allowed = {"ItemNumber", "ItemSource"}
    diffs: dict = {}
    for key in set(original) | set(payload):
        if key in allowed:
            continue
        before = original.get(key, _RELINK_MISSING)
        after = payload.get(key, _RELINK_MISSING)
        if before != after:
            diffs[key] = {
                "before": None if before is _RELINK_MISSING else before,
                "after": None if after is _RELINK_MISSING else after,
            }
    return diffs


@_tool()
def relink_order_line(
    order_id: str,
    row_id: str,
    channel_line_id: Optional[str] = None,
    channel_line_source: Optional[str] = None,
    allow_relink_of_linked_line: bool = False,
    location_id: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """
    Restore an orphaned order line's channel link (ItemNumber and ItemSource) with
    Orders/UpdateOrderItem. Use it on lines find_unlinked_order_lines reports as
    unlinked. Open orders only: a processed or despatched order is refused, because
    it can't be repaired this way.

    Live-proven on Default-location orders. There are two separate questions:
    whether the fields persist on Linnworks' order record, and whether
    despatch then maps the line back to its storefront line. Both are proven: ItemNumber
    persists, ItemSource persists for any non-empty value (an empty one is ignored
    behind a 200), and despatch matches the storefront line by ItemNumber. This
    tool's read-back answers the first question on every live run; only the
    storefront, after despatch, can answer the second.

    ⚠️ A WRONG channel_line_id is worse than a missing one: despatch then fulfils
    the WRONG storefront line, silently and successfully. So channel_line_id is
    required and never guessed. find_unlinked_order_lines can't catch a
    mis-pointed link afterwards (a line reads as linked once ItemSource is
    non-blank), so check the id against the storefront order before writing.

    ⚠️ LOCATION SCOPE: every proving order sat at the Default location, where the
    fulfilmentCenter this tool derives from the order is the zero GUID. Whether
    fulfilmentCenter's value matters at any other location has never been tested.
    The manifest shows the value that will be sent and the order's own location,
    and warns when either isn't Default or the two differ.

    Refused before any write:
      - an unknown row_id (get row ids from get_order);
      - a missing or blank channel_line_id;
      - a line whose ItemSource is already non-blank, unless
        allow_relink_of_linked_line=True, because a source-only fix makes a wrong
        line read as healthy;
      - a payload that would change anything other than ItemNumber and ItemSource.
        The line is sent back as its raw item with only those two fields changed,
        and any other difference (price, quantity, tax) aborts the write.

    After a live write the order is re-read and the outcome is "relinked",
    "not_persisted" (accepted but the fields didn't change), "unconfirmed" (the
    read-back failed or couldn't find the line) or "rate_limited". Any other field
    that changed is listed in `unexpected_field_changes`.

    dry_run defaults to True. This edits a real customer order, so review the
    manifest with the user before setting dry_run=False.

    Args:
        order_id: GUID pkOrderID or numeric order number.
        row_id: The line's row_id from get_order.
        channel_line_id: The correct storefront line id, written as ItemNumber.
            Required; never derived, defaulted or guessed.
        channel_line_source: Written as ItemSource. Defaults to the order's own
            Source; the manifest labels it "derived_from_order_source" or
            "supplied".
        allow_relink_of_linked_line: Must be True to write to a line whose
            ItemSource is already non-blank. Default False.
        location_id: fulfilmentCenter to send. Defaults to the order's own
            fulfilment location, then the Default location. Only Default is
            proven (see LOCATION SCOPE).
        dry_run: Default True: shows current vs intended values, writes nothing.

    Returns:
        On a refusal, an "error" key and nothing written. Otherwise: order_id,
        num_order_id, customer_name, reference_num, external_reference, row_id,
        line (sku, title, quantity), channel_line_id_before/after,
        channel_line_source_before/after, channel_line_source_origin,
        fulfilment_center (the value to be sent), fulfilment_center_is_default,
        order_fulfilment_location (the order's own), order_fulfilment_location_is_default,
        fulfilment_center_location_mismatch, fulfilment_center_warning; and after
        a live write: outcome, linnworks_response, unexpected_field_changes,
        unproven_warning.
    """
    order_id = order_id.strip()
    row_id = row_id.strip()

    try:
        guid, raw = _resolve_order_guid(order_id)
    except RateLimitError as exc:
        return {
            "outcome": "rate_limited",
            "error": f"rate_limited resolving order: {exc}",
            "order_id_input": order_id,
        }
    except RuntimeError as exc:
        return {"error": str(exc), "order_id_input": order_id}

    fmt = _format_order_detail(raw)

    if fmt.get("processed"):
        return {
            "error": (
                "This order is already processed (dispatched). A despatched "
                "order cannot be repaired this way."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    raw_items = list(raw.get("Items") or [])
    target = next((i for i in raw_items if i.get("RowId") == row_id), None)

    if target is None:
        return {
            "error": (
                f"row_id {row_id!r} not found on order {fmt.get('order_id')!r}. "
                "Use get_order() to list current line items and their row_id values."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "row_id": row_id,
        }

    channel_line_id = (channel_line_id or "").strip()
    if not channel_line_id:
        return {
            "error": (
                "channel_line_id is required and must not be blank. It is "
                "never derived, defaulted, or guessed by this tool — you "
                "must supply the correct storefront line id yourself. A "
                "guessed or mistyped id points despatch at the wrong "
                "storefront line, which is worse than leaving it blank."
            ),
            "reason": "channel_line_id_required",
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "row_id": row_id,
        }

    current_source = target.get("ItemSource")
    source_supplied = bool(channel_line_source and channel_line_source.strip())
    if source_supplied:
        effective_source = channel_line_source.strip()
        source_origin = "supplied"
    else:
        effective_source = fmt.get("source") or ""
        source_origin = "derived_from_order_source"

    if (current_source or "").strip() and not allow_relink_of_linked_line:
        return {
            "error": (
                f"This line's current channel_line_source is already "
                f"{current_source!r} (non-blank) — it does not look "
                "orphaned. relink_order_line refuses to write to an "
                "already-linked line by default, because "
                "find_unlinked_order_lines classifies purely on this field "
                "being non-blank and a source-only write would report a "
                "healthy line that never got the correct id. Set "
                "allow_relink_of_linked_line=True to override."
            ),
            "reason": "already_linked",
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "row_id": row_id,
            "channel_line_source_current": current_source,
        }

    fulfilment_centre = (
        location_id or raw.get("FulfilmentLocationId") or DEFAULT_LOCATION_ID
    )
    fulfilment_center_is_default = fulfilment_centre == DEFAULT_LOCATION_ID

    order_fulfilment_location = raw.get("FulfilmentLocationId") or DEFAULT_LOCATION_ID
    order_fulfilment_location_is_default = (
        order_fulfilment_location == DEFAULT_LOCATION_ID
    )
    fulfilment_center_location_mismatch = fulfilment_centre != order_fulfilment_location

    manifest = {
        "order_id": fmt.get("order_id"),
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "reference_num": fmt.get("reference_num"),
        "external_reference": fmt.get("external_reference"),
        "row_id": row_id,
        "line": {
            "sku": target.get("SKU"),
            "title": target.get("Title"),
            "quantity": target.get("Quantity"),
        },
        "channel_line_id_before": target.get("ItemNumber"),
        "channel_line_id_after": channel_line_id,
        "channel_line_source_before": current_source,
        "channel_line_source_after": effective_source,
        "channel_line_source_origin": source_origin,
        "fulfilment_center": fulfilment_centre,
        "fulfilment_center_is_default": fulfilment_center_is_default,
        "order_fulfilment_location": order_fulfilment_location,
        "order_fulfilment_location_is_default": order_fulfilment_location_is_default,
        "fulfilment_center_location_mismatch": fulfilment_center_location_mismatch,
    }
    if not order_fulfilment_location_is_default or fulfilment_center_location_mismatch:
        warning_parts = []
        if not order_fulfilment_location_is_default:
            warning_parts.append(
                "This order's own fulfilment location is NOT the Default "
                "location. Orders/UpdateOrderItem has only been proven at "
                "the Default location — whether "
                "fulfilmentCenter's value has any effect on this endpoint "
                f"elsewhere is {FULFILMENT_CENTER_EFFECT_OBSERVED}."
            )
        if fulfilment_center_location_mismatch:
            warning_parts.append(
                f"The fulfilmentCenter value about to be sent ({fulfilment_centre!r}) "
                "does NOT match this order's own fulfilment location "
                f"({order_fulfilment_location!r}). This exact mismatch has "
                "never been tested and its effect on Linnworks — a 400, a "
                "silent no-op, or a moved stock allocation — is completely "
                "unknown."
            )
        manifest["fulfilment_center_warning"] = " ".join(warning_parts)

    if dry_run:
        return {
            "dry_run": True,
            "status": "would_relink",
            "message": "Set dry_run=False to write these values to Orders/UpdateOrderItem.",
            **manifest,
        }


    def _live(result: dict) -> dict:
        return {**result, "unproven_warning": _RELINK_ORDER_LINE_UNPROVEN_WARNING}

    payload_item = _build_relink_order_item(target, channel_line_id, effective_source)
    unexpected_diff = _relink_payload_unexpected_diff(target, payload_item)
    if unexpected_diff:
        return _live({
            "dry_run": False,
            "error": (
                "Internal safety check failed: the constructed OrderItem "
                "payload differs from the line as read, on a key other "
                "than ItemNumber/ItemSource. Refusing to write — this "
                f"should never happen. Affected keys: {sorted(unexpected_diff.keys())}."
            ),
            "unexpected_diff": unexpected_diff,
            **manifest,
        })

    payload = {
        "orderId": guid,
        "orderItem": payload_item,
        "fulfilmentCenter": fulfilment_centre,
        "source": fmt.get("source"),
        "subSource": fmt.get("sub_source"),
    }

    try:
        write_resp = call_linnworks("Orders/UpdateOrderItem", payload)
    except RateLimitError as exc:
        return _live({
            "dry_run": False,
            "outcome": "rate_limited",
            "error": f"rate_limited on Orders/UpdateOrderItem: {exc}",
            **manifest,
        })

    try:
        _, raw_after = _resolve_order_guid(guid)
    except RateLimitError as exc:
        return _live({
            "dry_run": False,
            "outcome": "rate_limited",
            "error": f"rate_limited on read-back: {exc}",
            "linnworks_response": write_resp,
            **manifest,
        })
    except RuntimeError as exc:
        return _live({
            "dry_run": False,
            "outcome": "unconfirmed",
            "unconfirmed_reason": f"read-back failed: {exc}",
            "linnworks_response": write_resp,
            **manifest,
        })

    items_after = list(raw_after.get("Items") or [])
    item_after = next((i for i in items_after if i.get("RowId") == row_id), None)

    if item_after is None:
        return _live({
            "dry_run": False,
            "outcome": "unconfirmed",
            "unconfirmed_reason": (
                "The line could not be located on the fresh read-back "
                "after the write; this is not proof of success or failure."
            ),
            "linnworks_response": write_resp,
            **manifest,
        })

    id_after = item_after.get("ItemNumber")
    source_after = item_after.get("ItemSource")
    persisted = (id_after == channel_line_id) and (
        (source_after or "").strip() == (effective_source or "").strip()
    )
    outcome = "relinked" if persisted else "not_persisted"

    unexpected_field_changes = {}
    for key in set(target) | set(item_after):
        if key in ("ItemNumber", "ItemSource"):
            continue
        before = target.get(key, _RELINK_MISSING)
        after = item_after.get(key, _RELINK_MISSING)
        if before != after:
            unexpected_field_changes[key] = {
                "before": None if before is _RELINK_MISSING else before,
                "after": None if after is _RELINK_MISSING else after,
            }

    result = _live({
        "dry_run": False,
        "outcome": outcome,
        "linnworks_response": write_resp,
        "channel_line_id_after_write": id_after,
        "channel_line_source_after_write": source_after,
        "unexpected_field_changes": unexpected_field_changes,
        **manifest,
    })
    if outcome == "not_persisted":
        result["warning"] = (
            "Linnworks accepted the call but the fresh read-back shows the "
            "identity fields did not take the intended values. Check the "
            "order in the Linnworks UI before trying again."
        )
    return result


_CREATE_ORDER_RESERVED_SOURCES = {"AMAZON"}


def _is_reserved_source_refusal(message: str) -> bool:
    return "reserved for linnworks integrated channels" in (message or "").lower()


def _create_order_location_name(location: str) -> tuple[str | None, str | None]:
    value = (location or "").strip()
    if not value or value.lower() == DEFAULT_LOCATION_ID or value.lower() == "default":
        return "Default", None
    try:
        rows = call_linnworks_get("Inventory/GetStockLocations") or []
    except RateLimitError as exc:
        return None, f"Rate limited while resolving location '{value}' — nothing was created. ({exc})"
    except RuntimeError as exc:
        return None, f"Could not read the location list to resolve '{value}' — nothing was created. ({exc})"
    for row in rows if isinstance(rows, list) else []:
        guid = str(row.get("StockLocationId") or "").lower()
        name = row.get("LocationName") or ""
        if value.lower() in (guid, name.lower()):
            return name, None
    return None, (
        f"Location '{value}' is not one of this account's locations (by GUID or name). "
        "Nothing was created. Use get_locations to see the list."
    )


_CREATE_ORDER_UNPROVEN_WARNING = (
    "Orders/CreateOrders is live-proven in this account (paid, resend and unpaid, "
    "18 Sep 2026, Default location), but a 2xx only means Linnworks ACCEPTED "
    "this payload — it is not proof this order was created as intended. Check "
    "the read-back (state, total, lines) and the order in the Linnworks UI "
    "before letting it pick."
)

_ADDRESS_FIELD_MAP: dict[str, str] = {
    "full_name": "FullName",
    "company": "Company",
    "address1": "Address1",
    "address2": "Address2",
    "address3": "Address3",
    "town": "Town",
    "region": "Region",
    "postcode": "PostCode",
    "country": "Country",
    "phone": "PhoneNumber",
    "email": "EmailAddress",
}

_ADDRESS_REQUIRED: tuple[str, ...] = ("full_name", "address1", "town", "postcode", "country")

_CREATE_ORDER_PAYMENT_STATUSES: dict[str, str] = {
    "paid": "Paid",
    "unpaid": "Unpaid",
    "resend": "Paid",
}

_RESEND_STATUS_CODE = 4


def _parse_json_arg(raw: str, field: str, expect: type) -> tuple[object | None, str | None]:
    import json as _json

    if not isinstance(raw, str) or not raw.strip():
        return None, f"{field} is required and must be a non-empty JSON {expect.__name__}."
    try:
        parsed = _json.loads(raw)
    except Exception as exc:
        return None, f"Could not parse {field} JSON: {exc}"
    if not isinstance(parsed, expect):
        return None, f"{field} must be a JSON {expect.__name__}, got {type(parsed).__name__}."
    if expect is list and not parsed:
        return None, f"{field} must not be empty."
    return parsed, None


def _build_channel_address(addr: dict, label: str) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    missing = [f for f in _ADDRESS_REQUIRED if not str(addr.get(f, "") or "").strip()]
    if missing:
        errors.append(f"{label} is missing required field(s): {', '.join(missing)}.")

    unknown = sorted(set(addr) - set(_ADDRESS_FIELD_MAP))
    if unknown:
        errors.append(
            f"{label} has unrecognised field(s): {', '.join(unknown)}. "
            f"Valid fields: {', '.join(sorted(_ADDRESS_FIELD_MAP))}."
        )
    if errors:
        return None, errors

    out: dict = {}
    for snake, linn in _ADDRESS_FIELD_MAP.items():
        value = str(addr.get(snake, "") or "").strip()
        _check_injection(f"{label}.{snake}", value)
        out[linn] = value
    return out, []


def _lookup_postal_services() -> list[str]:
    rows = call_linnworks_get("Orders/GetShippingMethods")
    names: list[str] = []
    for vendor in rows if isinstance(rows, list) else []:
        for svc in (vendor.get("PostalServices") or []):
            name = svc.get("PostalServiceName")
            if name:
                names.append(name)
    return names


def _lookup_payment_methods() -> list[str]:
    rows = call_linnworks_get("Orders/GetPaymentMethods")
    return [r.get("Name") for r in (rows if isinstance(rows, list) else []) if r.get("Name")]


@_tool()
def create_order(
    items: str,
    delivery_address: str,
    billing_address: str = "",
    payment_status: str = "paid",
    source: str = "DIRECT",
    sub_source: str = "Phone",
    reference_number: str = "",
    external_reference: str = "",
    postal_service: str = "",
    payment_method: str = "",
    postage_cost: float = 0.0,
    prices_include_tax: bool = True,
    dispatch_by: str = "",
    currency: str = "GBP",
    note: str = "",
    location_id: str = DEFAULT_LOCATION_ID,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create a manual customer order in Linnworks (Orders/CreateOrders): phone or
    trade orders, and CS replacements and resends (e.g. qty 1 at £0.00 to a new
    address).

    ⚠️ CREATES A REAL, PICKABLE, DISPATCHABLE ORDER. dry_run defaults to True: it
    resolves every SKU, validates the postal service and payment method, checks
    for a duplicate reference and shows the exact order, writing nothing. Set
    dry_run=False only after reading that.

    Live-proven on the Default location for paid, resend and unpaid orders. A 2xx
    still only means Linnworks accepted the payload, not that THIS order is right,
    so a live result is reported "unconfirmed", never "success", with the order
    read back verbatim. Check it in the UI before it is picked.

    Refused before any write: an unknown SKU, postal service, payment method or
    location; a duplicate reference_number; unrecognised address fields; a bad
    dispatch_by.

    Args:
        items: JSON array of lines. Each needs "sku", "quantity" and "price"
            (per unit; £0.00 is valid). Optional: "tax_rate" (default 20.0),
            "title" (defaults to the item's title), "line_id" (defaults to the SKU;
            supply it when two lines share a SKU).
            Example: '[{"sku":"ABC-123","quantity":1,"price":0.00}]'
        delivery_address: JSON object. Required: full_name, address1, town,
            postcode, country. Optional: company, address2, address3, region,
            phone, email.
        billing_address: JSON object, same shape. Defaults to delivery_address.
        payment_status: "paid" (default) | "unpaid" | "resend".
            • "paid": the order lands in the dispatch queue.
            • "unpaid": ⚠️ Linnworks FORCE-PARKS it, whatever is sent, so it will
              NOT be in the dispatch queue. The manifest predicts this.
            • "resend": ⚠️ TWO WRITES. The order is created Paid, then set to
              RESEND with Orders/ChangeStatus. If the second call fails the order
              ALREADY EXISTS as Paid; the outcome is "created_but_status_not_set"
              and you must not re-run.
        source: Order source. Default "DIRECT" (a manual order). ⚠️ "AMAZON" is
            refused before any write, because Linnworks won't save it. Other real
            channels are untested: Linnworks may refuse them the same way, which
            is reported as an error with nothing created.
        sub_source: Label such as "Phone", "Trade" or "CS Replacement".
        reference_number: Your reference. A matching open order blocks the write,
            because CreateOrders silently skips an already-paid order on save, so
            a re-run would look successful and do nothing.
        external_reference: Link to an original order, e.g. the one being replaced.
        postal_service: Postal service NAME, validated against your list (a typo is
            refused, never created). Blank lets Linnworks assign one. The rules
            engine may reassign it anyway once the order reaches the queue; that is
            reported under `notes`.
        payment_method: Payment method NAME, validated the same way.
        postage_cost: Postage charged, inclusive of tax.
        prices_include_tax: Whether each line's "price" includes tax. Default True,
            verified live. ⚠️ False has never been sent live; check the totals.
        dispatch_by: Despatch deadline, ISO ("2026-09-19" or
            "2026-09-19T14:00:00"). Linnworks requires one; defaults to 1 day from
            now. It drives the overdue flag, so the current time makes the order
            instantly overdue.
        note: Internal note to add to the order.
        location_id: Fulfilment location, as a GUID or a name. Defaults to
            Default; translated to the location name Linnworks needs. Only
            "Default" is live-proven.
        confirmed_count: Orders over 10 LINES stage; echo back the line count.
        dry_run: Default True: shows the order without creating it.

    Returns:
        dry run: status "dry_run" with the full manifest.
        live run: status "unconfirmed" (never "success"), order_id, num_order_id,
            the read-back, notes and any warnings.
        blocked: status "error" or "blocked" with the reason, and no write made.
    """
    cache: dict = {}
    warnings: list[str] = []

    status_key = (payment_status or "").strip().lower()
    if status_key not in _CREATE_ORDER_PAYMENT_STATUSES:
        return {
            "status": "error",
            "error": (
                f"payment_status must be one of "
                f"{', '.join(sorted(_CREATE_ORDER_PAYMENT_STATUSES))} — got '{payment_status}'."
            ),
        }

    for field, value in (
        ("source", source),
        ("sub_source", sub_source),
        ("reference_number", reference_number),
        ("external_reference", external_reference),
        ("postal_service", postal_service),
        ("payment_method", payment_method),
        ("note", note),
        ("currency", currency),
    ):
        _check_injection(field, value or "")

    if dispatch_by.strip():
        try:
            dispatch_by_iso = datetime.fromisoformat(dispatch_by.strip()).isoformat()
        except ValueError:
            return {
                "status": "error",
                "error": (
                    f"dispatch_by '{dispatch_by}' is not a valid ISO date/time. "
                    'Use e.g. "2026-09-19" or "2026-09-19T14:00:00".'
                ),
            }
    else:
        dispatch_by_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    item_list, err = _parse_json_arg(items, "items", list)
    if err:
        return {"status": "error", "error": err}
    delivery_raw, err = _parse_json_arg(delivery_address, "delivery_address", dict)
    if err:
        return {"status": "error", "error": err}
    if billing_address and billing_address.strip():
        billing_raw, err = _parse_json_arg(billing_address, "billing_address", dict)
        if err:
            return {"status": "error", "error": err}
    else:
        billing_raw = delivery_raw

    delivery, addr_errors = _build_channel_address(delivery_raw, "delivery_address")
    billing, billing_errors = _build_channel_address(billing_raw, "billing_address")
    if addr_errors or billing_errors:
        return {"status": "error", "error": "; ".join(addr_errors + billing_errors)}

    if (source or "").strip().upper() in _CREATE_ORDER_RESERVED_SOURCES:
        return {
            "status": "error",
            "error": (
                f"source='{source}' cannot be used: Linnworks refuses to save a created order "
                "with that Source ('reserved for linnworks integrated channels', live-confirmed "
                "22 Sep 2026). Nothing was created. Use source='DIRECT' and put the original "
                "order in external_reference."
            ),
            "reserved_source": True,
        }

    location_name, loc_err = _create_order_location_name(location_id)
    if loc_err:
        return {"status": "error", "error": loc_err}

    resolved: list[dict] = []
    item_errors: list[dict] = []
    rate_limited = False
    seen_line_ids: set[str] = set()

    for idx, raw_item in enumerate(item_list):
        if not isinstance(raw_item, dict):
            item_errors.append({"index": idx, "error": "each item must be a JSON object."})
            continue
        sku = str(raw_item.get("sku", "") or "").strip()
        if not sku:
            item_errors.append({"index": idx, "error": "missing 'sku'."})
            continue
        try:
            quantity = int(raw_item.get("quantity", 0))
            price = float(raw_item.get("price"))
            tax_rate = float(raw_item.get("tax_rate", 20.0))
        except (TypeError, ValueError) as exc:
            item_errors.append({"index": idx, "sku": sku, "error": f"bad number: {exc}"})
            continue
        if quantity <= 0:
            item_errors.append({"index": idx, "sku": sku, "error": "quantity must be > 0."})
            continue
        if price < 0:
            item_errors.append({"index": idx, "sku": sku, "error": "price must be >= 0."})
            continue

        line_id = str(raw_item.get("line_id", "") or sku).strip()
        if line_id in seen_line_ids:
            item_errors.append({
                "index": idx,
                "sku": sku,
                "error": (
                    f"duplicate line id '{line_id}' — ChannelOrderItem.ItemNumber must be "
                    "unique within an order. Merge the quantities, or give each line its "
                    "own \"line_id\"."
                ),
            })
            continue
        seen_line_ids.add(line_id)

        title = str(raw_item.get("title", "") or "").strip()
        _check_injection(f"items[{idx}].title", title)
        _check_injection(f"items[{idx}].line_id", line_id)

        try:
            stock_item_id = _resolve_sku_to_id(sku, cache)
        except RateLimitError as exc:
            rate_limited = True
            item_errors.append({"index": idx, "sku": sku, "error": f"rate limited: {exc}"})
            continue
        except ValueError as exc:
            item_errors.append({"index": idx, "sku": sku, "error": str(exc)})
            continue

        if not title:
            try:
                title = (call_linnworks("Inventory/GetInventoryItem", {"sku": sku}) or {}).get(
                    "ItemTitle", ""
                ) or sku
            except RateLimitError:
                title = sku
                warnings.append(f"Rate limited reading the title for '{sku}' — used the SKU.")
            except RuntimeError:
                title = sku

        if prices_include_tax:
            line_total = round(quantity * price, 2)
            line_tax = round(line_total - (line_total / (1 + tax_rate / 100)), 2) if tax_rate else 0.0
        else:
            net = round(quantity * price, 2)
            line_tax = round(net * tax_rate / 100, 2)
            line_total = round(net + line_tax, 2)

        resolved.append({
            "sku": sku,
            "stock_item_id": stock_item_id,
            "title": title,
            "line_id": line_id,
            "quantity": quantity,
            "price_per_unit": price,
            "tax_rate": tax_rate,
            "line_tax": line_tax,
            "line_total_inc_tax": line_total,
        })

    if item_errors:
        out = {
            "status": "error",
            "error": (
                f"{len(item_errors)} of {len(item_list)} line(s) could not be used — "
                "no order was created. Fix them and retry."
            ),
            "item_errors": item_errors,
            "resolved_items": resolved,
        }
        if rate_limited:
            out["rate_limited"] = True
            out["error"] += (
                " NOTE: at least one failure was a rate limit (HTTP 429), NOT a missing "
                "SKU — retry shortly."
            )
        return out

    for label, value, lookup in (
        ("postal_service", postal_service, _lookup_postal_services),
        ("payment_method", payment_method, _lookup_payment_methods),
    ):
        if not (value or "").strip():
            continue
        try:
            known = lookup()
        except RateLimitError as exc:
            return {
                "status": "error",
                "rate_limited": True,
                "error": (
                    f"Rate limited (HTTP 429) while validating {label} — no order was "
                    f"created. This is a quota failure, not a bad {label}. Retry shortly. ({exc})"
                ),
            }
        except RuntimeError as exc:
            return {
                "status": "error",
                "error": f"Could not read the {label} list to validate '{value}': {exc}",
            }
        if value not in known:
            close = difflib.get_close_matches(value, known, n=5, cutoff=0.6)
            close += [k for k in known if value.lower() in k.lower() and k not in close]
            close = close[:8]
            return {
                "status": "error",
                "error": (
                    f"{label} '{value}' does not exist on this account, so the order was "
                    f"NOT created. Names are matched exactly."
                ),
                "did_you_mean": close,
                "available_count": len(known),
            }

    duplicate_of: list[dict] = []
    if reference_number.strip():
        try:
            found = find_orders_by_reference(reference_number.strip(), include_processed=False)
            for row in (found.get("orders") or []):
                duplicate_of.append({
                    "order_id": row.get("order_id"),
                    "num_order_id": row.get("num_order_id"),
                    "reference_num": row.get("reference_num"),
                })
        except RateLimitError:
            warnings.append(
                "Rate limited while checking for a duplicate reference — the duplicate "
                "check was SKIPPED, not passed. An order with this reference may already exist."
            )
        except Exception as exc:
            warnings.append(f"Duplicate-reference check could not run ({exc}); it was SKIPPED, not passed.")

    if duplicate_of:
        return {
            "status": "blocked",
            "error": (
                f"An open order with reference '{reference_number}' already exists — no order "
                "was created. Orders/CreateOrders silently skips an already-paid order on save, "
                "so creating another would look successful and do nothing. Use a different "
                "reference_number, or amend the existing order."
            ),
            "duplicate_of": duplicate_of,
        }

    linn_payment_status = _CREATE_ORDER_PAYMENT_STATUSES[status_key]
    items_total = round(sum(r["line_total_inc_tax"] for r in resolved), 2)
    order_total = round(items_total + float(postage_cost or 0.0), 2)

    if status_key == "unpaid":
        predicted_state = "PARKED"
        predicted_state_reason = (
            "Linnworks force-parks an unpaid order on save ('If Unpaid "
            "ChannelOrderAdapter.Save() will ensure order is PARKED'), whatever OrderState "
            "we send. It will NOT be in the dispatch queue."
        )
    else:
        predicted_state = "IN DISPATCH QUEUE"
        predicted_state_reason = "Paid orders are created live and pickable (OrderState 'None')."

    if _is_channel_order_source(source):
        warnings.append(
            f"source='{source}' is not DIRECT. Linnworks refused source 'AMAZON' as "
            "reserved for its integrated channels (live-confirmed 22 Sep 2026); "
            f"'{source}' is untested and may be refused the same way, in which case nothing "
            "is created. If it is accepted, the order's cost sits against it in reporting, "
            "but how its lines classify in find_unlinked_order_lines is unverified. Use "
            "source='DIRECT' unless you specifically want the channel attribution."
        )
    if status_key == "resend":
        warnings.append(
            "payment_status='resend' is TWO writes: create as Paid, then Orders/ChangeStatus(4). "
            "If the second fails, the order still exists and is live as Paid."
        )

    manifest = {
        "source": source,
        "sub_source": sub_source,
        "reference_number": reference_number,
        "external_reference": external_reference,
        "payment_status_requested": status_key,
        "payment_status_sent_to_linnworks": linn_payment_status,
        "predicted_state": predicted_state,
        "predicted_state_reason": predicted_state_reason,
        "postal_service": postal_service or "(Linnworks rules decide)",
        "payment_method": payment_method or "(Linnworks default)",
        "currency": currency,
        "dispatch_by": dispatch_by_iso,
        "location_id": location_id,
        "location_sent_to_linnworks": location_name,
        "prices_include_tax": prices_include_tax,
        "tax_note": (
            "prices_include_tax=True (TaxCostInclusive) is verified live: prices are read as "
            "tax-inclusive. False has never been sent live — check the totals if you use it."
            if prices_include_tax else
            "⚠️ prices_include_tax=False has never been sent live in this account (True is "
            "verified). Check the tax and totals on the created order."
        ),
        "delivery_address": delivery,
        "billing_address": billing,
        "lines": resolved,
        "line_count": len(resolved),
        "items_total_inc_tax": items_total,
        "postage_cost": round(float(postage_cost or 0.0), 2),
        "order_total_inc_tax": order_total,
        "note": note,
    }

    guard = _write_guard("create_order", resolved, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "warnings": warnings}

    if dry_run:
        return {
            "dry_run": True,
            "status": "dry_run",
            "manifest": manifest,
            "warnings": warnings,
            "unproven_endpoint_warning": _CREATE_ORDER_UNPROVEN_WARNING,
            "next_step": "Re-run with dry_run=False to create this order.",
        }

    channel_order: dict = {
        "Source": source,
        "SubSource": sub_source,
        "ReferenceNumber": reference_number,
        "ExternalReference": external_reference,
        "SecondaryReferenceNumber": "",
        "ChannelBuyerName": delivery.get("FullName", ""),
        "Currency": currency,
        "ReceivedDate": datetime.now(timezone.utc).isoformat(),
        "DispatchBy": dispatch_by_iso,
        "PaymentStatus": linn_payment_status,
        "OrderState": "None",
        "PostalServiceCost": round(float(postage_cost or 0.0), 2),
        "DeliveryAddress": delivery,
        "BillingAddress": billing,
        "AutomaticallyLinkBySKU": True,
        "UseChannelTax": True,
        "OrderItems": [
            {
                "ChannelSKU": r["sku"],
                "ItemNumber": r["line_id"],
                "ItemTitle": r["title"],
                "Qty": r["quantity"],
                "PricePerUnit": r["price_per_unit"],
                "TaxRate": r["tax_rate"],
                "TaxCostInclusive": prices_include_tax,
                "UseChannelTax": True,
                "IsService": False,
            }
            for r in resolved
        ],
    }
    if postal_service.strip():
        channel_order["MatchPostalServiceTag"] = postal_service
        channel_order["PostalServiceName"] = postal_service
        channel_order["SavePostalServiceIfNotExist"] = False
    if payment_method.strip():
        channel_order["MatchPaymentMethodTag"] = payment_method
        channel_order["PaymentMethodName"] = payment_method
        channel_order["SavePaymentMethodIfNotExist"] = False
    if note.strip():
        channel_order["Notes"] = [{
            "Note": note,
            "NoteEntryDate": datetime.now(timezone.utc).isoformat(),
            "Internal": True,
        }]

    payload = {"orders": [channel_order], "location": location_name}

    try:
        created = call_linnworks("Orders/CreateOrders", payload)
    except RateLimitError as exc:
        return {
            "dry_run": False,
            "status": "error",
            "rate_limited": True,
            "error": (
                f"Rate limited (HTTP 429) — the order was almost certainly NOT created, but "
                f"verify before retrying so you don't create it twice. ({exc})"
            ),
            "manifest": manifest,
        }
    except RuntimeError as exc:
        if _is_reserved_source_refusal(str(exc)):
            return {
                "dry_run": False,
                "status": "error",
                "reserved_source": True,
                "error": (
                    f"Linnworks refused source='{source}' as reserved for its integrated "
                    f"channels. Nothing was created. Use source='DIRECT' and put the original "
                    f"order in external_reference. ({exc})"
                ),
                "manifest": manifest,
            }
        return {
            "dry_run": False,
            "status": "error",
            "error": f"Orders/CreateOrders failed: {exc}",
            "payload_sent": payload,
            "hint": (
                "The unwrapped body and a location NAME are live-proven (v1.55.1), so neither "
                "is the likely cause. If the error mentions a missing parameter, the body may "
                "still need the {'request': {...}} wrapper for this case; if it mentions the "
                f"location, check '{location_name}' against get_locations."
            ),
            "manifest": manifest,
        }

    order_ids = created if isinstance(created, list) else (created.get("OrderIds") or [])
    if not order_ids:
        return {
            "dry_run": False,
            "status": "unconfirmed",
            "error": (
                "Orders/CreateOrders returned no order id. The call was accepted but nothing "
                "confirms an order exists — CHECK THE LINNWORKS UI before retrying, so you "
                "don't create a duplicate."
            ),
            "raw_response": created,
            "manifest": manifest,
        }

    order_guid = order_ids[0]
    result: dict = {
        "dry_run": False,
        "status": "unconfirmed",
        "order_id": order_guid,
        "manifest": manifest,
        "warnings": warnings,
        "unproven_endpoint_warning": _CREATE_ORDER_UNPROVEN_WARNING,
    }

    if status_key == "resend":
        try:
            call_linnworks(
                "Orders/ChangeStatus",
                {"orderIds": [order_guid], "status": _RESEND_STATUS_CODE},
            )
            result["resend_status_set"] = True
        except (RateLimitError, RuntimeError) as exc:
            result["resend_status_set"] = False
            result["outcome"] = "created_but_status_not_set"
            result["error"] = (
                f"⚠️ THE ORDER WAS CREATED AND IS LIVE, but the follow-up "
                f"Orders/ChangeStatus(4) failed, so it is still marked PAID rather than "
                f"RESEND. Order {order_guid} exists in the dispatch queue right now. "
                f"Fix the status in the Linnworks UI — do NOT re-run this tool, that would "
                f"create a second order. ({exc})"
            )

    try:
        _, raw = _resolve_order_guid(order_guid)
        detail = _format_order_detail(raw)
        result["read_back"] = {
            "num_order_id": detail.get("num_order_id"),
            "status": detail.get("status"),
            "status_label": _PAYMENT_STATUS_LABELS.get(detail.get("status"), "UNKNOWN"),
            "is_parked": detail.get("is_parked"),
            "processed": detail.get("processed"),
            "source": detail.get("source"),
            "sub_source": detail.get("sub_source"),
            "reference_num": detail.get("reference_num"),
            "postal_service_name": detail.get("postal_service_name"),
            "totals": detail.get("totals"),
            "line_count": len(detail.get("items") or []),
            "lines_linked": [
                {
                    "sku": i.get("SKU"),
                    "stock_item_id": i.get("StockItemId"),
                    "channel_line_id": i.get("channel_line_id"),
                    "channel_line_source": i.get("channel_line_source"),
                }
                for i in (detail.get("items") or [])
            ],
        }
        result["num_order_id"] = detail.get("num_order_id")

        actual_service = detail.get("postal_service_name")
        if postal_service.strip() and actual_service and actual_service != postal_service:
            result.setdefault("notes", []).append(
                f"Postal service reassigned by the Linnworks rules engine: requested "
                f"'{postal_service}', the order carries '{actual_service}'. This is "
                f"expected — the rules engine assigns shipping on incoming orders — so "
                f"treat the requested service as a preference, not a guarantee."
            )
        result["read_back"]["postal_service_requested"] = postal_service or None
        result["read_back"]["postal_service_reassigned"] = bool(
            postal_service.strip() and actual_service and actual_service != postal_service
        )

        actual_parked = detail.get("is_parked")
        if actual_parked is True and predicted_state != "PARKED":
            result.setdefault("warnings", []).append(
                "The order was PARKED, which is not what the manifest predicted. It is NOT in "
                "the dispatch queue."
            )
        elif actual_parked is False and predicted_state == "PARKED":
            result.setdefault("warnings", []).append(
                "The order was NOT parked, though an unpaid order was expected to be. It may "
                "be live in the dispatch queue — check it."
            )
    except RateLimitError as exc:
        result["read_back"] = None
        result.setdefault("warnings", []).append(
            f"Rate limited reading the order back — it WAS created ({order_guid}), but nothing "
            f"here confirms its state. Check the Linnworks UI. ({exc})"
        )
    except RuntimeError as exc:
        result["read_back"] = None
        result.setdefault("warnings", []).append(
            f"The order was created ({order_guid}) but could not be read back: {exc}. "
            "Check the Linnworks UI."
        )

    result.setdefault("outcome", "created")
    result["next_step"] = (
        "Verify the order in the Linnworks UI before it picks. If this was a test, cancel it "
        "with cancel_order()."
    )
    return result


_SHIPPING_QUOTE_PRIME_TAG = "AMAZON_PRIME"


def _fetch_shipping_integrations() -> list[dict]:
    resp = call_linnworks_get("ShippingService/GetIntegrations")
    return resp if isinstance(resp, list) else []


def _format_shipping_quote(q: dict) -> dict:
    return {
        "vendor": q.get("Vendor"),
        "friendly_name": q.get("FriendlyName"),
        "account_id": q.get("AccountId"),
        "service_name": q.get("ServiceName"),
        "service_code": q.get("ServiceCode"),
        "service_id": q.get("ServiceId"),
        "cost": q.get("Cost"),
        "tax": q.get("Tax"),
        "total_cost": q.get("TotalCost"),
        "currency": q.get("Currency"),
        "collection_date": q.get("CollectionDate"),
        "estimated_delivery_date": q.get("EstimatedDeliveryDate"),
        "properties": [
            {"title": p.get("Title"), "value": p.get("Value")}
            for p in (q.get("PropertyItem") or [])
            if p.get("Title") or p.get("Value")
        ],
        "options": [
            {"name": o.get("OptionName"), "value": o.get("OptionValue")}
            for o in (q.get("Options") or [])
        ],
    }


@_tool()
def get_shipping_quote(order_id: str, accounts: Optional[list[str]] = None) -> dict:
    """
    Get live shipping rate quotes for one order from its shipping integrations.

    READ-ONLY. Quoting only asks the carriers for prices: it buys no label and
    does not change the order's shipping method. Use it to compare services
    (e.g. Amazon Buy Shipping vs other carriers) before choosing one.

    By default every installed integration with quoting enabled is asked
    (for example Amazon Buy Shipping). Pass `accounts`
    to narrow it — each entry is matched case-insensitively against the
    integration's vendor code, friendly name, account id, or config id
    (e.g. "Amazon Buy Shipping", "AMAZONSHIPPING_V2", "9"). An integration
    whose quoting is disabled can still be requested; it will usually come
    back under `errors` rather than `quotes`.

    Amazon Buy Shipping quotes Amazon orders only; on a Shopify/eBay/DIRECT
    order expect an error for that account. The response reports whether the
    order carries the AMAZON_PRIME tag. On a Prime order Amazon returns only
    Prime-compliant services (verified 30 Sep 2026: 3 next-day services on a
    Prime order, against 19 on a non-Prime order). Amazon Buy Shipping takes
    ~50 seconds per quote. To act on a quote, use accept_shipping_quote.

    Args:
        order_id: GUID pkOrderID or numeric order number.
        accounts: optional list of integrations to quote against.

    Returns:
        A dict with order_id, num_order_id, source, sub_source, is_prime,
        current_postal_service, accounts_quoted, quote_count, cheapest,
        quotes (sorted by total_cost ascending), errors (per-account failures,
        verbatim), and warnings.
    """
    try:
        guid, order = _resolve_order_guid(str(order_id))
    except RuntimeError as exc:
        return {"error": str(exc), "order_id": order_id}

    general = order.get("GeneralInfo") or {}
    shipping = order.get("ShippingInfo") or {}
    source = general.get("Source") or ""
    tags = [
        (i.get("Tag") or "").upper()
        for i in (general.get("Identifiers") or [])
        if isinstance(i, dict)
    ]
    is_prime = _SHIPPING_QUOTE_PRIME_TAG in tags

    integrations = _fetch_shipping_integrations()
    real = [i for i in integrations if i.get("AccountId")]
    warnings: list[str] = []

    if accounts:
        selected, unknown = [], []
        for want in accounts:
            w = str(want).strip().lower()
            match = [
                i for i in real
                if w in {
                    str(i.get("Vendor") or "").lower(),
                    str(i.get("VendorFriendlyName") or "").lower(),
                    str(i.get("AccountId") or "").lower(),
                    str(i.get("pkShippingAPIConfigId")),
                }
            ]
            if not match:
                unknown.append(want)
            for m in match:
                if m not in selected:
                    selected.append(m)
        if unknown:
            return {
                "error": f"Unknown shipping account(s): {unknown}. Nothing was quoted.",
                "order_id": guid,
                "available_accounts": [
                    {
                        "config_id": i.get("pkShippingAPIConfigId"),
                        "vendor": i.get("Vendor"),
                        "friendly_name": i.get("VendorFriendlyName"),
                        "account_id": i.get("AccountId"),
                        "quote_enabled": bool(i.get("QuoteEnabled")),
                    }
                    for i in real
                ],
            }
        for s in selected:
            if not s.get("QuoteEnabled"):
                warnings.append(
                    f"Quoting is disabled on '{s.get('VendorFriendlyName')}' in Linnworks; "
                    "it will probably return an error rather than a quote."
                )
    else:
        selected = [i for i in real if i.get("QuoteEnabled")]
        if not selected:
            return {
                "error": "No shipping integration on this account has quoting enabled.",
                "order_id": guid,
            }

    if order.get("Processed"):
        warnings.append("This order is already processed; carriers may refuse to quote it.")
    if any(str(s.get("Vendor") or "").upper().startswith("AMAZONSHIPPING") for s in selected) \
            and source.upper() != "AMAZON":
        warnings.append(
            f"Amazon Buy Shipping only quotes Amazon orders; this order's source is "
            f"'{source or 'blank'}', so expect an error for that account."
        )
    address = ((order.get("CustomerInfo") or {}).get("Address")) or {}
    country = str(address.get("Country") or "").strip()
    if not country or country.upper() == "UNKNOWN":
        warnings.append(
            "The order has no delivery country yet (Linnworks shows "
            f"'{country or 'blank'}'), so carriers cannot price it. Seen live on a fresh "
            "Amazon order whose address had not arrived; retry once the address is present."
        )
    if is_prime:
        warnings.append(
            "This is a Prime order. Amazon returns only Prime-compliant services for it "
            "(verified 30 Sep 2026: 3 next-day services vs 19 on a non-Prime order), so "
            "expect a short list."
        )

    payload_accounts = [
        {
            "Vendor": s.get("Vendor"),
            "AccountId": s.get("AccountId"),
            "VendorFriendlyName": s.get("VendorFriendlyName"),
        }
        for s in selected
    ]
    resp = call_linnworks(
        "ShippingService/GetShippingQuote",
        {"request": {"pkOrderId": guid, "Accounts": payload_accounts}},
    )
    resp = resp if isinstance(resp, dict) else {}

    quotes = sorted(
        (_format_shipping_quote(q) for q in (resp.get("Quotes") or [])),
        key=lambda q: (q["total_cost"] is None, q["total_cost"] or 0),
    )
    errors = [
        {
            "vendor": e.get("Vendor"),
            "friendly_name": e.get("FriendlyName") or next(
                (a["VendorFriendlyName"] for a in payload_accounts
                 if a["Vendor"] == e.get("Vendor") and a["AccountId"] == e.get("AccountId")),
                None,
            ),
            "account_id": e.get("AccountId"),
            "message": e.get("ErrorMessage"),
        }
        for e in (resp.get("Errors") or [])
    ]

    if not quotes and not errors:
        warnings.append(
            "No quotes and no errors were returned — Linnworks gave no reason. "
            "This happened live on an already-processed order."
        )

    return {
        "order_id": guid,
        "num_order_id": order.get("NumOrderId"),
        "reference": general.get("ReferenceNum"),
        "source": source,
        "sub_source": general.get("SubSource"),
        "is_prime": is_prime,
        "current_postal_service": shipping.get("PostalServiceName"),
        "accounts_quoted": [a["VendorFriendlyName"] or a["Vendor"] for a in payload_accounts],
        "quote_count": len(quotes),
        "cheapest": quotes[0] if quotes else None,
        "quotes": quotes,
        "errors": errors,
        "warnings": warnings,
        "note": "Quotes only — no label was bought and the order's shipping method is unchanged.",
    }


def _postal_services_by_integration() -> tuple[dict[str, list[dict]], dict[str, int]]:
    by_int: dict[str, list[dict]] = {}
    name_counts: dict[str, int] = {}
    for vendor in call_linnworks_get("Orders/GetShippingMethods") or []:
        for ps in vendor.get("PostalServices") or []:
            name = ps.get("PostalServiceName") or ""
            if name:
                key = name.strip().lower()
                name_counts[key] = name_counts.get(key, 0) + 1
            integ = (ps.get("IntegratedServiceID") or "").lower()
            if integ:
                by_int.setdefault(integ, []).append({
                    "postal_service_id": ps.get("pkPostalServiceId"),
                    "postal_service_name": name,
                    "vendor": vendor.get("Vendor"),
                })
    return by_int, name_counts


def _match_quote_service(quote: dict, want: str) -> bool:
    w = want.strip().lower()
    return w in {
        str(quote.get("service_name") or "").lower(),
        str(quote.get("service_code") or "").lower(),
        str(quote.get("service_id") or "").lower(),
    }


@_tool()
def accept_shipping_quote(
    order_ids: list[str],
    service: Optional[str] = None,
    accounts: Optional[list[str]] = None,
    confirmed_count: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """
    Accept a shipping quote for one or more open orders by setting each order's
    shipping service to the quoted service. Defaults to the CHEAPEST quote.

    What "accept" means: the order's postal service is changed (via
    Orders/ChangeShippingMethod) to the Linnworks postal service linked to the
    quoted carrier service. NO LABEL IS BOUGHT HERE — Linnworks buys the label
    when it is printed/processed, at the carrier's price at that time, which
    can differ from the quote.

    Each order is re-quoted fresh (see get_shipping_quote), so this is slow:
    Amazon Buy Shipping takes ~50 seconds per order. Keep batches small.

    Choosing the quote:
      - service=None → the cheapest quote that maps to a Linnworks postal
        service. A cheaper quote with no matching postal service is reported
        under `cheaper_unmappable` and skipped, never guessed.
      - service="..." → the quote whose service name, code or id matches.
    On a Prime order Amazon only returns Prime-compliant services (verified
    30 Sep 2026: 3 next-day services vs 19 on a non-Prime order), so the
    cheapest Prime quote still keeps the Prime delivery promise.

    Refused per order, before any write: unresolvable order, processed order,
    a label already printed or a tracking number already present (changing
    service would orphan a bought label), no quotes returned, no quote matching
    `service`, no quote with a matching postal service, or a postal service
    name shared by several services (the endpoint takes a NAME, so it would be
    ambiguous). An order already on the chosen service is a no-op.

    Args:
        order_ids: GUIDs or numeric order numbers of OPEN orders.
        service: optional quote to pick instead of the cheapest.
        accounts: optional integrations to quote against (as get_shipping_quote).
        confirmed_count: echo the order count to confirm a batch above the
            staging threshold (25).
        dry_run: default True — shows the plan and writes nothing.

    Returns:
        plan (per order: current service, chosen quote, target postal service,
        action/blocked reason), and on a live run `results` with a fresh
        read-back per order: outcome changed / not_changed / unconfirmed /
        rate_limited / error.
    """
    if isinstance(order_ids, str):
        order_ids = [order_ids]
    if service is not None:
        _check_injection("service", service)

    try:
        by_int, name_counts = _postal_services_by_integration()
    except RateLimitError as exc:
        return {"error": f"Rate-limited reading postal services; nothing was changed. {exc}",
                "rate_limited": True}

    plan: list[dict] = []
    for oid in order_ids:
        row: dict = {"input": oid, "blocked": True}
        plan.append(row)
        try:
            guid, order = _resolve_order_guid(str(oid))
        except RateLimitError as exc:
            row.update(reason="rate_limited", detail=str(exc))
            continue
        except RuntimeError as exc:
            row.update(reason="order_not_found", detail=str(exc))
            continue
        general = order.get("GeneralInfo") or {}
        shipping = order.get("ShippingInfo") or {}
        row.update(
            order_id=guid,
            num_order_id=order.get("NumOrderId"),
            reference=general.get("ReferenceNum"),
            source=general.get("Source"),
            current_postal_service=shipping.get("PostalServiceName"),
        )
        if order.get("Processed"):
            row["reason"] = "processed"
            continue
        if general.get("LabelPrinted") or (shipping.get("TrackingNumber") or "").strip():
            row.update(reason="label_already_printed",
                       detail="A label or tracking number already exists; changing the service would orphan it.")
            continue
        try:
            quote = get_shipping_quote(guid, accounts=accounts)
        except RateLimitError as exc:
            row.update(reason="rate_limited", detail=str(exc))
            continue
        if quote.get("error"):
            row.update(reason="quote_failed", detail=quote["error"])
            continue
        row["is_prime"] = quote.get("is_prime")
        row["quote_errors"] = quote.get("errors")
        quotes = quote.get("quotes") or []
        if not quotes:
            row.update(reason="no_quotes", detail="; ".join(quote.get("warnings") or []) or None)
            continue

        candidates = [q for q in quotes if _match_quote_service(q, service)] if service else quotes
        if not candidates:
            row.update(reason="service_not_quoted",
                       available=[q["service_name"] for q in quotes])
            continue

        chosen, target, skipped = None, None, []
        for q in candidates:
            matches = by_int.get(str(q.get("service_id") or "").lower()) or []
            if len(matches) == 1:
                chosen, target = q, matches[0]
                break
            skipped.append({
                "service_name": q["service_name"], "total_cost": q["total_cost"],
                "why": "no Linnworks postal service is linked to this quote" if not matches
                       else f"{len(matches)} postal services are linked to this quote",
            })
        row["cheaper_unmappable"] = skipped
        if chosen is None:
            row["reason"] = "no_mappable_quote"
            continue
        if name_counts.get(target["postal_service_name"].strip().lower(), 0) > 1:
            row.update(reason="ambiguous_postal_service_name",
                       detail=f"'{target['postal_service_name']}' names more than one postal service.")
            continue

        row.update(
            blocked=False,
            chosen_quote={k: chosen[k] for k in ("service_name", "service_code", "total_cost",
                                                  "currency", "estimated_delivery_date")},
            target_postal_service=target["postal_service_name"],
            target_postal_service_id=target["postal_service_id"],
            reason=None,
        )
        same = (str(shipping.get("PostalServiceId") or "").lower()
                == str(target["postal_service_id"] or "").lower())
        row["action"] = "no_op" if same else "change"

    to_change = [r for r in plan if not r["blocked"] and r["action"] == "change"]
    summary = {
        "order_count": len(order_ids),
        "to_change": len(to_change),
        "no_op": sum(1 for r in plan if not r["blocked"] and r["action"] == "no_op"),
        "blocked": sum(1 for r in plan if r["blocked"]),
        "note": "Setting the shipping service buys no label; the label is bought when "
                "printed, at the carrier's price then.",
    }

    guard = _write_guard("accept_shipping_quote", to_change, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **summary, "plan": plan}
    if dry_run:
        return {"dry_run": True, **summary, "plan": plan}

    results = []
    for r in to_change:
        res = {"order_id": r["order_id"], "num_order_id": r["num_order_id"],
               "target_postal_service": r["target_postal_service"]}
        try:
            resp = call_linnworks("Orders/ChangeShippingMethod", {
                "orderIds": [r["order_id"]],
                "shippingMethod": r["target_postal_service"],
            })
            res["response"] = resp or None
        except RateLimitError as exc:
            res.update(outcome="rate_limited", detail=f"Not sent: {exc}")
            results.append(res)
            continue
        except RuntimeError as exc:
            res.update(outcome="error", detail=str(exc))
            results.append(res)
            continue
        try:
            _, after = _resolve_order_guid(r["order_id"])
            ship = after.get("ShippingInfo") or {}
            res["postal_service_after"] = ship.get("PostalServiceName")
            ok = (str(ship.get("PostalServiceId") or "").lower()
                  == str(r["target_postal_service_id"] or "").lower())
            res["outcome"] = "changed" if ok else "not_changed"
        except RateLimitError:
            res.update(outcome="rate_limited",
                       detail="The change was sent and may have landed; the read-back was throttled. Do not re-run blindly.")
        except RuntimeError as exc:
            res.update(outcome="unconfirmed", detail=f"Change sent; read-back failed: {exc}")
        results.append(res)

    return {"dry_run": False, **summary, "plan": plan, "results": results,
            "changed": sum(1 for x in results if x.get("outcome") == "changed")}


@_tool()
def set_order_status(
    order_ids: list[str],
    action: str,
    confirmed_count: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """
    Change the workflow status of one or more orders. Actions (any case):
      - "lock": hold the order so it CANNOT be picked or despatched (padlock in
        the UI; it stays in the open-orders queue). Use this to hold an order,
        e.g. while a cancellation request is confirmed.
      - "unlock": release a held order.
      - "paid" / "unpaid": set the payment status (Orders/ChangeStatus 1 / 0).

    ⚠️ LOCKING RELEASES THE ORDER'S ALLOCATED STOCK, so it can be sold on other
    orders while this one is held. Parking would keep it allocated, but "park"
    and "unpark" aren't available through the API and are refused with a pointer
    to "lock".

    ⚠️ A lock can't be read back (the API has no lock field): the result is the
    write response, and you should check the padlock in the UI. Paid/unpaid are
    read back.

    ⚠️ Whether marking an EXISTING order unpaid PARKS it (taking it out of the
    dispatch queue) has not been observed. An order created unpaid is parked, but
    that is a different code path. The unpaid manifest states the prediction and
    its basis, and the live read-back reports `is_parked`, warning if it differs.

    Every order is resolved first and its status, parked and processed flags,
    customer and reference go into the manifest. An unresolvable id becomes a
    `resolve_errors` row and never aborts the batch. All resolved orders change in
    one API call. Batches over 25 stage.

    Args:
        order_ids: GUIDs or numeric order numbers (a single string is accepted).
        action: "lock", "unlock", "paid" or "unpaid".
        confirmed_count: For batches over 25, echo the exact order count.
        dry_run: Default True: returns the manifest and changes nothing.

    Returns:
        dry_run, action, endpoint, order_count, resolved_count, the manifest (dry
        run) or per-order results (live), resolve_errors, and rate_limited.
        A live paid/unpaid row carries status_after, status_after_label, changed,
        parked_before, is_parked, parked_changed, and a warning if parking
        contradicts the prediction. readback="rate_limited" means the write WAS
        sent: treat it as unconfirmed, check the UI, and don't re-run.
    """
    action_norm = (action or "").strip().lower()

    if action_norm in _UNSUPPORTED_STATUS_ACTIONS:
        return {
            "error": (
                f"Action '{action_norm}' is not supported — the Linnworks public "
                f"API has no park/unpark endpoint (parking exists only as a "
                f"RulesEngine action; GeneralInfo.IsParked is read-only). To hold "
                f"an order from dispatch, use action='lock' instead."
            ),
            "action": action_norm,
            "supported_actions": list(_ORDER_STATUS_ACTIONS.keys()),
        }

    if action_norm not in _ORDER_STATUS_ACTIONS:
        return {
            "error": (
                f"Unknown action {action!r}. Supported actions: "
                f"{', '.join(_ORDER_STATUS_ACTIONS.keys())}."
            ),
            "supported_actions": list(_ORDER_STATUS_ACTIONS.keys()),
        }

    if isinstance(order_ids, str):
        order_ids = [order_ids]
    order_ids = [str(o).strip() for o in (order_ids or []) if str(o).strip()]
    if not order_ids:
        return {"error": "No order_ids provided.", "action": action_norm}

    endpoint, value = _ORDER_STATUS_ACTIONS[action_norm]

    resolved: list[tuple[str, dict]] = []
    manifest: list[dict] = []
    resolve_errors: list[dict] = []
    rate_limited: list[dict] = []
    guid_list: list[str] = []

    for oid in order_ids:
        try:
            guid, raw = _resolve_order_guid(oid)
        except RateLimitError as exc:
            rate_limited.append({"order_id_input": oid, "stage": "resolve",
                                 "error": str(exc)})
            manifest.append({"order_id_input": oid, "resolved": False,
                             "rate_limited": True, "error": str(exc)})
            continue
        except RuntimeError as exc:
            resolve_errors.append({"order_id_input": oid, "error": str(exc)})
            manifest.append({"order_id_input": oid, "resolved": False, "error": str(exc)})
            continue

        fmt = _format_order_detail(raw)
        cur = fmt.get("status")
        row = {
            "order_id_input":       oid,
            "order_id":             guid,
            "num_order_id":         fmt.get("num_order_id"),
            "customer_name":        fmt.get("customer_name"),
            "reference_num":        fmt.get("reference_num"),
            "external_reference":   fmt.get("external_reference"),
            "source":               fmt.get("source"),
            "processed":            fmt.get("processed"),
            "is_parked":            fmt.get("is_parked"),
            "current_status":       cur,
            "current_status_label": _PAYMENT_STATUS_LABELS.get(cur, f"Unknown({cur})"),
            "action":               action_norm,
            "resolved":             True,
        }

        if endpoint == "LockOrder":
            row["intent"] = (
                "Lock (hold from picking/dispatch)" if value
                else "Unlock (release for processing)"
            )
            if fmt.get("processed"):
                row["warning"] = (
                    "Order is already processed/dispatched — locking has no "
                    "effect on a dispatched order."
                )
        else:
            row["new_status"] = value
            row["new_status_label"] = _PAYMENT_STATUS_LABELS.get(value)
            row["intent"] = f"Set payment status → {_PAYMENT_STATUS_LABELS.get(value)}"
            if action_norm == "unpaid":
                predicted_parked, predicted_parked_reason = _unpaid_parks_prediction()
                row["predicted_parked"] = predicted_parked
                row["predicted_parked_reason"] = predicted_parked_reason

        resolved.append((guid, row))
        guid_list.append(guid)
        manifest.append(row)

    guard = _write_guard("set_order_status", order_ids, confirmed_count, dry_run)
    if guard is not None:
        return {
            **guard,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "manifest": manifest,
            "resolve_errors": resolve_errors,
            "rate_limited": rate_limited,
        }

    if dry_run:
        return {
            "dry_run": True,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "order_count": len(order_ids),
            "resolved_count": len(guid_list),
            "manifest": manifest,
            "resolve_errors": resolve_errors,
            "rate_limited": rate_limited,
            "message": "Set dry_run=False to execute this status change.",
        }

    if not guid_list:
        return {
            "dry_run": False,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "order_count": len(order_ids),
            "resolved_count": 0,
            "results": [],
            "resolve_errors": resolve_errors,
            "rate_limited": rate_limited,
            "message": "No resolvable orders to act on.",
        }

    if endpoint == "LockOrder":
        payload = {"orderIds": guid_list, "lockOrder": bool(value)}
    else:
        payload = {"orderIds": guid_list, "status": int(value)}
    write_resp = call_linnworks(f"Orders/{endpoint}", payload)

    results: list[dict] = []
    for guid, row in resolved:
        rb = {
            "order_id":     guid,
            "num_order_id": row["num_order_id"],
            "reference_num": row["reference_num"],
            "action":       action_norm,
        }
        try:
            _, raw2 = _resolve_order_guid(guid)
            fmt2 = _format_order_detail(raw2)
            if endpoint == "ChangeStatus":
                new = fmt2.get("status")
                rb["status_after"] = new
                rb["status_after_label"] = _PAYMENT_STATUS_LABELS.get(new, f"Unknown({new})")
                rb["changed"] = (new == value)

                parked_before = row.get("is_parked")
                parked_after = fmt2.get("is_parked")
                rb["parked_before"] = parked_before
                rb["is_parked"] = parked_after
                rb["parked_changed"] = (parked_after != parked_before)

                predicted = row.get("predicted_parked")
                if predicted is True and parked_after is False:
                    rb.setdefault("warnings", []).append(
                        "The order was NOT parked, though an unpaid flip was expected to "
                        "park it. It may be live in the dispatch queue — check it, and "
                        "correct UNPAID_FLIP_PARKS_ORDER if this repeats."
                    )
                elif predicted is False and parked_after is True:
                    rb.setdefault("warnings", []).append(
                        "The order was PARKED, which is not what was predicted. It is NOT "
                        "in the dispatch queue — check it, and correct "
                        "UNPAID_FLIP_PARKS_ORDER if this repeats."
                    )
            else:
                rb["is_parked"] = fmt2.get("is_parked")
                rb["lock_readback"] = "unavailable"
                rb["note"] = (
                    "The Linnworks API exposes no lock-status field, so the lock "
                    "cannot be verified by read-back — confirm the padlock in the "
                    "Linnworks UI."
                )
        except RateLimitError as exc:
            rb["readback"] = "rate_limited"
            rb["note"] = (
                f"Rate limited reading this order back. The write was already sent and "
                f"may well have succeeded — nothing here confirms it either way. Do NOT "
                f"re-run this tool on the assumption it failed; check the order in the "
                f"Linnworks UI. ({exc})"
            )
        except RuntimeError as exc:
            rb["readback_error"] = str(exc)
        results.append(rb)

    return {
        "dry_run": False,
        "action": action_norm,
        "endpoint": f"Orders/{endpoint}",
        "order_count": len(order_ids),
        "resolved_count": len(guid_list),
        "linnworks_response": write_resp,
        "results": results,
        "resolve_errors": resolve_errors,
        "rate_limited": rate_limited,
    }


@_tool()
def refund_order(
    order_id: str,
    note: Optional[str] = None,
    push_to_channel: bool = True,
    dry_run: bool = True,
) -> dict:
    """
    Issue a full refund for a processed Linnworks order.

    Refunds all items and postage. The order must already be processed
    (dispatched). For open orders, use cancel_order instead.

    Flow when dry_run=False:
      1. Fetches the order to get items, totals, and OrderItemRowIds.
      2. Calls GetRefundOptions to confirm the order can be refunded.
      3. Creates a refund record via ReturnsRefunds/CreateRefund.
      4. If push_to_channel=True, actions the refund via ReturnsRefunds/ActionRefund,
         which pushes the refund to the sales channel (Shopify, Amazon, eBay, etc.).
      5. Re-reads the order's refund headers (ReturnsRefunds/GetRefundHeadersByOrderId)
         and reports whether the created refund is now visible there.

    PROVEN LIVE (18 Sep 2026, the post-merge verification):
    ReturnsRefunds/CreateRefund creates a real refund header, and the header
    is then visible via GetRefundHeadersByOrderId — the same read-back this
    tool now performs after every live refund. The eligibility gate
    (_check_refund_eligibility) has also been proven, both refusing and
    permitting correctly against real order data.

    NOT PROVEN: ReturnsRefunds/ActionRefund — the call that pushes a refund
    to the sales channel — has never been shown to actually reach a channel.
    It cannot be proven on a throwaway order (a manually created DIRECT order
    has no channel to push to); the only proof is refunding a genuine
    Shopify/Amazon/eBay order and watching that channel's own admin. Every
    live response that attempts a channel push (push_to_channel=True) carries
    this warning under "channel_push_warning" — see REFUND_CHANNEL_PUSH_WARNING,
    the single place this text is written.

    IMPORTANT: dry_run defaults to True. Refunds move real customer money —
    always confirm the order and refund amounts before setting dry_run=False.

    Args:
        order_id: Order to refund. Accepts a GUID pkOrderID or numeric order number.
        note: Optional note/reason to include with the refund.
        push_to_channel: If True (default), also submits the refund to the sales
            channel (Shopify, Amazon, etc.). If False, records the refund in
            Linnworks only.
        dry_run: If True (default), shows the refund that would be created
            without creating it. Set to False to execute.

    Returns:
        A dict with order details, per-line refund amounts, total refund, and
        if live:
          - refund_header_id, refund_reference, status: from CreateRefund.
          - actioned: True/False from ActionRefund's SuccessfullyActioned —
            keeps its existing name, type and meaning unchanged.
          - actioned_with_errors: True when actioned is True but ActionRefund
            also returned a non-empty Errors array — a contradiction
            Linnworks itself does not reconcile. Carries its own warning.
          - channel_push_warning: present whenever a channel push was
            attempted (push_to_channel=True and a refund was created) and
            REFUND_CHANNEL_PUSH_PROVEN is False.
          - outcome: "confirmed" (the refund header is visible on a fresh
            read-back), "unconfirmed" (the read-back succeeded but no
            matching header was found yet — see unconfirmed_message; NEVER
            re-run the refund on this outcome), "read_back_failed" (the
            read-back call itself failed), or "rate_limited" (a
            RateLimitError was raised during the read-back — distinct from
            every other outcome).
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if not fmt.get("processed"):
        return {
            "error": (
                "This order is not yet processed. "
                "Use cancel_order for open (unprocessed) orders."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    refund_lines: list[dict] = []
    for item in (raw.get("Items") or []):
        row_id = item.get("RowId")
        if not row_id:
            continue
        line: dict = {
            "OrderItemRowId": row_id,
            "RefundedUnit": "Item",
            "Amount": float(item.get("CostIncTax") or 0.0),
        }
        if note:
            line["FreeTextOrNote"] = note
        refund_lines.append(line)

    totals_raw = raw.get("TotalsInfo") or {}
    postage = float(totals_raw.get("PostageCost") or 0.0)
    if postage > 0:
        shipping_line: dict = {"RefundedUnit": "Shipping", "Amount": postage}
        if note:
            shipping_line["FreeTextOrNote"] = note
        refund_lines.append(shipping_line)

    total_refund = sum(rl["Amount"] for rl in refund_lines)
    currency = totals_raw.get("Currency")

    if dry_run:
        return {
            "dry_run": True,
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "customer_name": fmt.get("customer_name"),
            "customer_email": fmt.get("customer_email"),
            "reference_num": fmt.get("reference_num"),
            "external_reference": fmt.get("external_reference"),
            "total_refund": total_refund,
            "currency": currency,
            "push_to_channel": push_to_channel,
            "refund_lines": [
                {
                    "unit":              rl.get("RefundedUnit"),
                    "amount":            rl.get("Amount"),
                    "order_item_row_id": rl.get("OrderItemRowId"),
                }
                for rl in refund_lines
            ],
            "note": note,
            "message": "Set dry_run=False to execute this full refund.",
        }

    options_resp = _get_refund_options(guid)
    options = options_resp.get("RefundOptions") or {}
    eligibility_error = _check_refund_eligibility(options, push_to_channel)
    if eligibility_error:
        return {
            "error": eligibility_error,
            "order_id": guid,
            "num_order_id": fmt.get("num_order_id"),
            "can_refund": options.get("CanRefund"),
            "can_refund_internally": options.get("CanRefundInternally"),
        }

    create_resp = call_linnworks(
        "ReturnsRefunds/CreateRefund",
        {
            "request": {
                "OrderId": guid,
                "ChannelInitiated": False,
                "RefundLines": refund_lines,
            }
        },
    )
    refund_header_id = create_resp.get("RefundHeaderId")
    create_errors = create_resp.get("Errors") or []
    cannot = create_resp.get("CannotRefundReason") or "None"

    if cannot != "None" or create_errors:
        return {
            "error": (
                f"Refund creation failed. "
                f"Reason: {cannot}. Errors: {create_errors}"
            ),
            "order_id": guid,
            "create_response": create_resp,
        }

    result: dict = {
        "dry_run": False,
        "order_id": guid,
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "refund_header_id": refund_header_id,
        "refund_reference": create_resp.get("RefundReference"),
        "total_refund": total_refund,
        "currency": currency,
        "status": (create_resp.get("Status") or {}),
        "push_to_channel": push_to_channel,
        "actioned": False,
    }

    if push_to_channel and refund_header_id is not None:
        action_resp = call_linnworks(
            "ReturnsRefunds/ActionRefund",
            {"request": {"RefundHeaderId": refund_header_id, "OrderId": guid}},
        )
        result["actioned"] = action_resp.get("SuccessfullyActioned", False)
        result["action_status"] = (action_resp.get("Status") or {})
        result["action_errors"] = action_resp.get("Errors") or []
        result["actioned_with_errors"] = bool(
            result["actioned"] and result["action_errors"]
        )
        if result["actioned_with_errors"]:
            result["warning"] = (
                "ActionRefund reported SuccessfullyActioned=True but also "
                "returned a non-empty Errors array. Linnworks has not "
                "reconciled this itself — do not treat SuccessfullyActioned "
                "alone as proof the refund reached the channel."
            )
        push_warning = _refund_channel_push_warning(
            (raw.get("GeneralInfo") or {}).get("Source"))
        if push_warning:
            result["channel_push_warning"] = push_warning

    _apply_refund_readback(result, guid, refund_header_id)
    return result


@_tool()
def refund_order_lines(
    order_id: str,
    lines: list,
    refund_postage: bool = False,
    note: Optional[str] = None,
    push_to_channel: bool = True,
    dry_run: bool = True,
) -> dict:
    """
    Partially refund specific lines on a processed order. Call get_order first
    for each line's `row_id`, then pass only the lines to refund.

    ⚠️ Refunds move real customer money. dry_run defaults to True.

    ⚠️ Passing `quantity` without an `amount` is REFUSED when that quantity isn't
    the line's full quantity, because the amount would otherwise default to the
    whole line (quantity=1 on a 3-unit line would refund all 3). Pass an explicit
    `amount` for a part-quantity refund.

    PROVEN LIVE: creating the refund (CreateRefund), the eligibility gate, and the
    read-back. The channel push (ActionRefund) is proven live on Shopify for a
    full refund. NOT PROVEN: ActionRefund for a partial refund like this one, the
    postage component, or any Amazon or eBay order, where a push has been accepted
    before without reaching the channel. A live response that pushes to an
    unproven channel carries `channel_push_warning`.

    After a live refund the order's refund headers are re-read. `outcome` is
    "confirmed", "unconfirmed" (no matching header yet; NEVER re-run the refund
    on this, it risks paying twice), "read_back_failed" or "rate_limited".

    Args:
        order_id: GUID or numeric order number.
        lines: List of dicts with row_id (required, from get_order), amount
            (optional; defaults to the line's full cost_inc_tax) and quantity
            (optional; see the warning above).
        refund_postage: Also refund the postage.
        note: Optional reason for the refund.
        push_to_channel: Default True: submit the refund to the sales channel.
            False records it in Linnworks only.
        dry_run: Default True: shows the refund without making it.

    Returns:
        The refund summary, and on a live run: refund_header_id,
        refund_reference, status, actioned (ActionRefund's SuccessfullyActioned),
        actioned_with_errors (actioned but Linnworks also returned errors),
        channel_push_warning, outcome and unconfirmed_message.
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if not fmt.get("processed"):
        return {
            "error": (
                "This order is not yet processed. "
                "Use cancel_order for open (unprocessed) orders."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    item_by_row_id: dict[str, dict] = {
        i["RowId"]: i
        for i in (raw.get("Items") or [])
        if i.get("RowId")
    }

    refund_lines: list[dict] = []
    unknown_row_ids: list[str] = []
    quantity_mismatch_lines: list[dict] = []

    for ln in (lines or []):
        row_id = ln.get("row_id")
        if not row_id:
            return {"error": "Each entry in 'lines' must have a 'row_id' field."}
        item = item_by_row_id.get(row_id)
        if item is None:
            unknown_row_ids.append(row_id)
            continue

        explicit_amount = ln.get("amount") is not None
        quantity = ln.get("quantity")
        line_quantity = item.get("Quantity")
        if (
            quantity is not None
            and not explicit_amount
            and line_quantity is not None
            and int(quantity) != int(line_quantity)
        ):
            quantity_mismatch_lines.append({
                "row_id": row_id,
                "sku": item.get("SKU"),
                "requested_quantity": quantity,
                "line_quantity": line_quantity,
                "line_cost_inc_tax": item.get("CostIncTax"),
            })
            continue

        amount = float(ln["amount"]) if explicit_amount else float(
            item.get("CostIncTax") or 0.0
        )
        rl: dict = {
            "OrderItemRowId": row_id,
            "RefundedUnit": "Item",
            "Amount": amount,
        }
        if quantity is not None:
            rl["Quantity"] = int(quantity)
        if note:
            rl["FreeTextOrNote"] = note
        refund_lines.append(rl)

    if unknown_row_ids:
        return {
            "error": (
                f"row_id(s) not found in order items: {unknown_row_ids}. "
                "Use get_order() to list valid row_ids for this order."
            ),
            "order_id": guid,
        }

    if quantity_mismatch_lines:
        return {
            "error": (
                "Refusing to refund a partial quantity without an explicit "
                "amount — this would silently refund the WHOLE line's cost "
                "for only some of its units, over-refunding the customer. "
                f"Pass an explicit 'amount' for these line(s): "
                f"{quantity_mismatch_lines}"
            ),
            "order_id": guid,
            "quantity_mismatch_lines": quantity_mismatch_lines,
        }

    totals_raw = raw.get("TotalsInfo") or {}
    postage = float(totals_raw.get("PostageCost") or 0.0)
    if refund_postage and postage > 0:
        shipping_line: dict = {"RefundedUnit": "Shipping", "Amount": postage}
        if note:
            shipping_line["FreeTextOrNote"] = note
        refund_lines.append(shipping_line)

    if not refund_lines:
        return {"error": "No valid refund lines to process."}

    total_refund = sum(rl["Amount"] for rl in refund_lines)
    currency = totals_raw.get("Currency")

    if dry_run:
        return {
            "dry_run": True,
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "customer_name": fmt.get("customer_name"),
            "customer_email": fmt.get("customer_email"),
            "reference_num": fmt.get("reference_num"),
            "external_reference": fmt.get("external_reference"),
            "total_refund": total_refund,
            "currency": currency,
            "push_to_channel": push_to_channel,
            "refund_lines": [
                {
                    "unit":              rl.get("RefundedUnit"),
                    "amount":            rl.get("Amount"),
                    "order_item_row_id": rl.get("OrderItemRowId"),
                    "quantity":          rl.get("Quantity"),
                }
                for rl in refund_lines
            ],
            "note": note,
            "message": "Set dry_run=False to execute this partial refund.",
        }

    options_resp = _get_refund_options(guid)
    options = options_resp.get("RefundOptions") or {}
    eligibility_error = _check_refund_eligibility(options, push_to_channel)
    if eligibility_error:
        return {
            "error": eligibility_error,
            "order_id": guid,
            "num_order_id": fmt.get("num_order_id"),
            "can_refund": options.get("CanRefund"),
            "can_refund_internally": options.get("CanRefundInternally"),
        }

    create_resp = call_linnworks(
        "ReturnsRefunds/CreateRefund",
        {
            "request": {
                "OrderId": guid,
                "ChannelInitiated": False,
                "RefundLines": refund_lines,
            }
        },
    )
    refund_header_id = create_resp.get("RefundHeaderId")
    create_errors = create_resp.get("Errors") or []
    cannot = create_resp.get("CannotRefundReason") or "None"

    if cannot != "None" or create_errors:
        return {
            "error": (
                f"Refund creation failed. "
                f"Reason: {cannot}. Errors: {create_errors}"
            ),
            "order_id": guid,
            "create_response": create_resp,
        }

    result: dict = {
        "dry_run": False,
        "order_id": guid,
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "refund_header_id": refund_header_id,
        "refund_reference": create_resp.get("RefundReference"),
        "total_refund": total_refund,
        "currency": currency,
        "status": (create_resp.get("Status") or {}),
        "push_to_channel": push_to_channel,
        "actioned": False,
    }

    if push_to_channel and refund_header_id is not None:
        action_resp = call_linnworks(
            "ReturnsRefunds/ActionRefund",
            {"request": {"RefundHeaderId": refund_header_id, "OrderId": guid}},
        )
        result["actioned"] = action_resp.get("SuccessfullyActioned", False)
        result["action_status"] = (action_resp.get("Status") or {})
        result["action_errors"] = action_resp.get("Errors") or []
        result["actioned_with_errors"] = bool(
            result["actioned"] and result["action_errors"]
        )
        if result["actioned_with_errors"]:
            result["warning"] = (
                "ActionRefund reported SuccessfullyActioned=True but also "
                "returned a non-empty Errors array. Linnworks has not "
                "reconciled this itself — do not treat SuccessfullyActioned "
                "alone as proof the refund reached the channel."
            )
        push_warning = _refund_channel_push_warning(
            (raw.get("GeneralInfo") or {}).get("Source"))
        if push_warning:
            result["channel_push_warning"] = push_warning

    _apply_refund_readback(result, guid, refund_header_id)
    return result


@_tool()
def get_stock_level(
    sku: str,
    location_id: str = DEFAULT_LOCATION_ID,
    include_empty_locations: bool = False,
) -> dict:
    """
    Return the current stock level for a SKU, optionally at a specific location.

    Performs a two-step lookup: resolves the SKU to a StockItemId, then fetches
    stock levels across all locations.

    Useful for questions like "how much stock do we have of SKU ABC-123?" or
    "what's the available quantity at our main warehouse?".

    Note: returns StockLevel (gross on-hand), Available (net of order-book
    allocations), InOrderBook, and Due. For FIFO purposes use StockLevel,
    not Available.

    In many accounts, some locations are virtual dropship supplier locations
    rather than physical warehouses. When multiple non-Default locations show the same
    non-zero stock_level, that value likely represents the supplier's available
    quantity (not additional owned stock). The "Default" location is the
    authoritative owned-stock figure.

    Args:
        sku: The exact SKU / item number to look up (case-insensitive).
        location_id: The Linnworks StockLocationId to filter to. Defaults to
            the "Default" location (all-locations aggregate). Pass a specific
            location GUID to see a single warehouse row.
        include_empty_locations: If True, return all 27 location rows including
            those with zero stock. Defaults to False (only non-zero rows).

    Returns:
        A dict with the item identity and a list of stock level rows per location,
        plus a notes field when potential duplicate supplier counts are detected.
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    levels_response = call_linnworks(
        "Stock/GetStockLevel_Batch",
        {"request": {"StockItemIds": [stock_item_id]}},
    )

    batch = levels_response if isinstance(levels_response, list) else []
    item_row = next((r for r in batch if r.get("pkStockItemId") == stock_item_id), None)
    levels = item_row.get("StockItemLevels") or [] if item_row else []

    if location_id != DEFAULT_LOCATION_ID:
        levels = [
            l for l in levels
            if (l.get("Location") or {}).get("StockLocationId") == location_id
        ]

    formatted = [
        {
            "location_name": (l.get("Location") or {}).get("LocationName"),
            "location_id": (l.get("Location") or {}).get("StockLocationId"),
            "stock_level": l.get("StockLevel"),
            "available": l.get("Available"),
            "in_order_book": l.get("InOrderBook"),
            "due": l.get("Due"),
        }
        for l in levels
    ]

    if not include_empty_locations:
        formatted = [f for f in formatted if (f["stock_level"] or 0) > 0]

    notes = []
    non_default = [f for f in formatted if f["location_id"] != DEFAULT_LOCATION_ID and (f["stock_level"] or 0) > 0]
    if len(non_default) > 1:
        level_values = [f["stock_level"] for f in non_default]
        if len(set(level_values)) == 1:
            notes.append(
                f"{len(non_default)} non-Default locations all show stock_level={level_values[0]}. "
                "These are likely virtual dropship supplier locations showing the same supplier "
                "availability — not additional owned stock. Use the 'Default' row for owned inventory."
            )

    result = {
        "sku": sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "location_id_filter": location_id,
        "levels": formatted,
    }
    if notes:
        result["notes"] = notes
    return result


_CHANGE_SOURCE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("RETURN",       ("customer return for order",)),
    ("SCRAP",        ("scrapped by",)),
    ("SALE",         ("order ",)),
    ("PO_DELIVERY",  ("po delivered", "delivered")),
    ("STOCKTAKE",    ("stock count adjustment",)),
    ("ADJUSTMENT",   ("direct adjustment",)),
    ("FBA_SYNC",     ("fba sync", "no quantity returned from channel")),
    ("FILE_IMPORT",  ("imported from file", "import ")),
]

_AUTOMATED_CHANGE_SOURCES = ("FILE_IMPORT", "FBA_SYNC")

_KNOWN_CHANGE_SOURCES = (
    "SALE", "RETURN", "PO_DELIVERY", "PO_BOOKING", "PO_DELETED", "PO_UPDATE",
    "STOCKTAKE", "ADJUSTMENT", "FBA_SYNC", "FILE_IMPORT", "SCRAP", "OTHER",
)


def _classify_change_source(note: str | None) -> str:
    n = (note or "").strip().lower()
    if not n:
        return "OTHER"

    if n.startswith("po "):
        if "delivered" in n:
            return "PO_DELIVERY"
        if "to open" in n or "due " in n:
            return "PO_BOOKING"
        if "deleted" in n:
            return "PO_DELETED"
        if "update" in n:
            return "PO_UPDATE"
        return "OTHER"

    for source, prefixes in _CHANGE_SOURCE_PATTERNS:
        for p in prefixes:
            if n.startswith(p) or p in n:
                return source
    return "OTHER"


def _fetch_change_history_rows(
    stock_item_id: str,
    location_id: str,
    max_pages: int,
    per_page: int = 200,
) -> tuple[list[dict], int, bool]:
    rows: list[dict] = []
    total = 0
    page = 1
    while page <= max_pages:
        resp = call_linnworks_get(
            "Stock/GetItemChangesHistory",
            {
                "stockItemId": stock_item_id,
                "locationId": location_id,
                "entriesPerPage": per_page,
                "pageNumber": page,
            },
        )
        total = resp.get("TotalEntries") or 0
        batch = resp.get("Data") or []
        rows.extend(batch)
        if len(batch) < per_page:
            return rows, total, False
        page += 1
    return rows, total, len(rows) < total


def _format_change_row(row: dict) -> dict:
    note = row.get("Note")
    return {
        "date": row.get("Date"),
        "change_source": _classify_change_source(note),
        "level_after": row.get("Level"),
        "quantity": row.get("ChangeQty"),
        "stock_value_after": row.get("StockValue"),
        "change_value": row.get("ChangeValue"),
        "note": note,
    }


def _summarise_change_history(
    rows: list[dict],
    current_level: int | None,
    truncated: bool,
    now: "datetime | None" = None,
) -> dict:
    now = now or datetime.now(timezone.utc)

    def _parse(d: str | None):
        if not d:
            return None
        try:
            return datetime.fromisoformat(str(d).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _first(pred):
        return next((r for r in rows if pred(r)), None)

    out_of_stock_since = None
    zero_run_rows = 0
    zero_run_complete = True
    if rows and (rows[0].get("level_after") or 0) == 0:
        for r in rows:
            if (r.get("level_after") or 0) != 0:
                break
            out_of_stock_since = r.get("date")
            zero_run_rows += 1
        if zero_run_rows == len(rows) and truncated:
            zero_run_complete = False

    level_from_history = rows[0].get("level_after") if rows else None
    level_mismatch = (
        current_level is not None
        and level_from_history is not None
        and current_level != level_from_history
    )
    if current_level is not None and current_level > 0:
        out_of_stock_since = None

    days_out = None
    since_dt = _parse(out_of_stock_since)
    if since_dt is not None:
        days_out = (now - since_dt).days

    movements = [r for r in rows if (r.get("quantity") or 0) != 0]
    real_movements = [
        r for r in movements
        if r.get("change_source") not in _AUTOMATED_CHANGE_SOURCES
    ]
    last_sale = _first(lambda r: r.get("change_source") == "SALE")
    last_recv = _first(lambda r: r.get("change_source") in ("PO_DELIVERY", "RETURN"))

    return {
        "current_level": current_level,
        "level_from_history": level_from_history,
        "out_of_stock_since": out_of_stock_since,
        "days_out_of_stock": days_out,
        "out_of_stock_since_is_lower_bound": bool(out_of_stock_since) and not zero_run_complete,
        "last_movement_date": movements[0]["date"] if movements else None,
        "last_real_movement_date": real_movements[0]["date"] if real_movements else None,
        "last_sale_date": last_sale["date"] if last_sale else None,
        "last_received_date": last_recv["date"] if last_recv else None,
        "movement_count": len(rows),
        "level_mismatch": level_mismatch,
    }


@_tool()
def get_stock_change_history(
    skus: list[str] | str,
    location_id: str | None = None,
    all_locations: bool = False,
    include_movements: bool = True,
    max_movements: int = 50,
    change_sources: list[str] | None = None,
    exclude_change_sources: list[str] | None = None,
    max_pages: int = 10,
) -> dict:
    """
    Stock movement history for one or more SKUs, plus a summary answering "when
    did this item actually go out of stock?". Each movement has its date,
    quantity, resulting level and source (sale, PO delivery, return, stocktake,
    adjustment, file import, FBA sync). Use it to age zero-stock items before
    retiring them: an item at zero for two days is not dead stock.

    `out_of_stock_since` is the date the item went TO zero: the oldest row in the
    current unbroken run at zero, not the latest zero row (automated rows keep
    rewriting 0 on long-dead items, which would reset the clock to today).
    `days_out_of_stock` follows from it.

    ⚠️ PER LOCATION. Linnworks needs a location for history, so there is no
    combined figure. all_locations=True scans every location and returns a
    per-location breakdown plus `zero_at_all_locations_since` (the LATEST of the
    per-location zero dates, set only when every location is at zero). Quantities
    are never summed across locations, because supplier-mirror locations can show
    the same stock twice.

    ⚠️ change_source is DERIVED from the free-text note (Linnworks returns no such
    field). The raw `note` is always included; reclassify anything in "OTHER".

    Filters apply to `movements` and `last_real_movement_date` only, never to the
    level series behind `out_of_stock_since`: automated rows can be real
    decrements, and dropping them would give a wrong date.

    Cost: one call per (SKU, location) page; all_locations=True costs one call per
    location for each SKU, so keep those batches small.

    Args:
        skus: One SKU or a list (exact, ACTIVE items only; archived SKUs can't be
            resolved).
        location_id: Location GUID. Defaults to Default. Ignored with
            all_locations.
        all_locations: Scan every location (see above).
        include_movements: Default True. False for a summary-only screen.
        max_movements: Cap on returned rows per SKU/location (default 50). The
            summary still covers every row read.
        change_sources: Keep only these sources: SALE, RETURN, PO_DELIVERY,
            PO_BOOKING, PO_DELETED, PO_UPDATE, STOCKTAKE, ADJUSTMENT, FBA_SYNC,
            FILE_IMPORT, SCRAP, OTHER.
        exclude_change_sources: Drop these sources (applied after change_sources).
        max_pages: History pages (200 rows each) per SKU/location, default 10. If
            the zero run reaches the end of a cut-off read,
            `out_of_stock_since_is_lower_bound` is True.

    Returns:
        Per-SKU results with out_of_stock_since, days_out_of_stock,
        last_movement_date, last_real_movement_date, last_sale_date,
        last_received_date, optional movements, and with all_locations a
        `locations` breakdown. Unresolvable SKUs go to `unresolved`; throttled
        ones to `rate_limited`, with `complete: false`.
    """
    if isinstance(skus, str):
        skus = [skus]
    if not skus:
        return {"error": "No SKUs supplied.", "count": 0, "results": []}

    for name, sel in (("change_sources", change_sources),
                      ("exclude_change_sources", exclude_change_sources)):
        if sel:
            bad = [s for s in sel if s.upper() not in _KNOWN_CHANGE_SOURCES]
            if bad:
                raise ValueError(
                    f"Unknown {name}: {bad}. Valid sources: {', '.join(_KNOWN_CHANGE_SOURCES)}"
                )

    include_set = {s.upper() for s in change_sources} if change_sources else None
    exclude_set = {s.upper() for s in exclude_change_sources} if exclude_change_sources else set()
    max_pages = max(1, min(int(max_pages or 1), 100))

    if all_locations:
        loc_rows = call_linnworks_get("Inventory/GetStockLocations") or []
        targets = [
            (l.get("StockLocationId"), l.get("LocationName"))
            for l in loc_rows
            if l.get("StockLocationId")
        ]
    else:
        lid = location_id or DEFAULT_LOCATION_ID
        targets = [(lid, None)]

    def _apply_filter(rows: list[dict]) -> list[dict]:
        out = rows
        if include_set is not None:
            out = [r for r in out if r["change_source"] in include_set]
        if exclude_set:
            out = [r for r in out if r["change_source"] not in exclude_set]
        return out

    results, unresolved, rate_limited = [], [], []
    id_cache: dict = {}
    calls = 0

    for sku in skus:
        try:
            stock_item_id = _resolve_sku_to_id(sku, id_cache)
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except Exception as exc:
            unresolved.append({
                "sku": sku,
                "error": str(exc),
                "hint": "Archived items cannot be resolved by SKU — unarchive first.",
            })
            continue

        levels_by_loc: dict[str, int] = {}
        try:
            batch = call_linnworks(
                "Stock/GetStockLevel_Batch", {"request": {"StockItemIds": [stock_item_id]}}
            )
            calls += 1
            item_row = next(
                (r for r in (batch if isinstance(batch, list) else [])
                 if r.get("pkStockItemId") == stock_item_id),
                None,
            )
            for l in (item_row or {}).get("StockItemLevels") or []:
                loc = (l.get("Location") or {}).get("StockLocationId")
                if loc:
                    levels_by_loc[loc] = l.get("StockLevel") or 0
        except RateLimitError as exc:
            rate_limited.append({
                "sku": sku,
                "error": f"stock levels unavailable: {exc}",
                "note": "ageing skipped — a missing level would fake an out-of-stock date",
            })
            continue
        except Exception:
            levels_by_loc = {}

        per_location = []
        for lid, lname in targets:
            rows_raw, total, truncated = _fetch_change_history_rows(
                stock_item_id, lid, max_pages
            )
            calls += max(1, -(-len(rows_raw) // 200))
            rows = [_format_change_row(r) for r in rows_raw]
            summary = _summarise_change_history(
                rows, levels_by_loc.get(lid), truncated
            )
            entry = {
                "location_id": lid,
                "location_name": lname,
                "total_entries": total,
                "rows_read": len(rows),
                "history_truncated": truncated,
                **summary,
            }
            if include_movements:
                shown = _apply_filter(rows)
                entry["movements"] = shown[:max_movements]
                entry["movements_truncated"] = len(shown) > max_movements
                entry["movements_matching_filter"] = len(shown)
            per_location.append(entry)

        record: dict = {"sku": sku, "stock_item_id": stock_item_id}

        if all_locations:
            active = [p for p in per_location if p["total_entries"] or p["current_level"]]
            all_zero = all((p["current_level"] or 0) == 0 for p in per_location)
            zero_dates = [p["out_of_stock_since"] for p in active if p["out_of_stock_since"]]
            never_stocked = all_zero and not zero_dates
            record.update({
                "scope": "all_locations",
                "zero_at_all_locations": all_zero,
                "zero_at_all_locations_since": max(zero_dates) if (all_zero and zero_dates) else None,
                "never_stocked_anywhere": never_stocked,
                "locations_with_history": len([p for p in per_location if p["total_entries"]]),
                "locations": [p for p in per_location if p["total_entries"] or (p["current_level"] or 0)],
            })
        else:
            record.update({"scope": "location", **per_location[0]})

        results.append(record)

    return {
        "count": len(results),
        "unresolved_count": len(unresolved),
        "rate_limited_count": len(rate_limited),
        "complete": not rate_limited,
        "scope": "all_locations" if all_locations else "location",
        "change_source_filter": {
            "include": sorted(include_set) if include_set else None,
            "exclude": sorted(exclude_set) if exclude_set else None,
            "note": (
                "Filters apply to `movements` and last_real_movement_date only — never "
                "to the level series behind out_of_stock_since."
            ),
        },
        "automated_sources": list(_AUTOMATED_CHANGE_SOURCES),
        "api_calls": calls,
        "results": results,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
    }


@_tool()
def get_processed_orders(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 500,
) -> dict:
    """
    List processed (dispatched/fulfilled) orders from Linnworks within a date range.

    Processed orders are orders that have been marked as dispatched — they no longer
    appear in the open orders queue. Use this to answer questions like "what orders
    did we ship last week?", "how many orders were processed yesterday?", or
    "how many orders came in during May?".

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
            Interpreted as midnight UTC on that date.
        to_date: End of the date range in ISO format, e.g. "2026-05-08".
            Interpreted as 23:59:59 UTC on that date.
        date_field: Which date to filter on. One of: "received" (default),
            "processed", "payment", "cancelled". Use "processed" for dispatch date,
            "received" for order received date.
        page: Page number for paginated results. Defaults to 1.
        page_size: Orders per page. Min 20, defaults to 500 (recommended for large
            date ranges to minimise round-trips).

    Returns:
        A dict with:
          - from_date, to_date:  the date range queried
          - date_field:          which date field was filtered
          - page, total_pages:   pagination info
          - total_count:         total matching orders across all pages
          - count:               number of orders returned on this page
          - orders:              list of processed order summaries
    """
    page_size = max(20, page_size)

    payload = {
        "request": {
            "DateField": date_field,
            "FromDate": f"{from_date}T00:00:00",
            "ToDate": f"{to_date}T23:59:59",
            "PageNumber": page,
            "ResultsPerPage": page_size,
        }
    }

    response = call_linnworks("ProcessedOrders/SearchProcessedOrders", payload)

    wrapper = response.get("ProcessedOrders") or {}
    raw_orders = wrapper.get("Data") or []
    total_count = wrapper.get("TotalEntries", 0)
    total_pages = wrapper.get("TotalPages", 1)

    orders = [
        {
            "order_id": o.get("pkOrderID"),
            "num_order_id": o.get("nOrderId"),
            "reference_num": o.get("ReferenceNum"),
            "external_reference": o.get("ExternalReference"),
            "source": o.get("Source"),
            "sub_source": o.get("SubSource"),
            "received_date": o.get("dReceivedDate"),
            "processed_date": o.get("dProcessedOn"),
            "total_charge": o.get("fTotalCharge"),
            "currency": o.get("cCurrency"),
            "postal_service_name": o.get("PostalServiceName"),
            "tracking_number": o.get("PostalTrackingNumber"),
            "country": o.get("cCountry"),
            "fulfilment_location": o.get("FulfilmentLocationName"),
        }
        for o in raw_orders
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(orders),
        "orders": orders,
    }


@_tool()
def get_locations() -> dict:
    """
    List all stock locations configured in Linnworks.

    Returns every warehouse, fulfilment centre, and virtual location with its
    GUID, name, and type flags. Useful for resolving location names to IDs
    before calling tools that accept a location_id parameter, and for answering
    questions like "which locations do we have?" or "what is the GUID for our
    main warehouse?".

    Returns:
        A dict with:
          - count:     total number of locations
          - locations: list of location records
    """
    locations = call_linnworks_get("Inventory/GetStockLocations")

    return {
        "count": len(locations),
        "locations": [
            {
                "location_id": loc.get("StockLocationId"),
                "name": loc.get("LocationName"),
                "is_fulfillment_center": loc.get("IsFulfillmentCenter"),
                "is_warehouse_managed": loc.get("IsWarehouseManaged"),
                "is_not_trackable": loc.get("IsNotTrackable"),
                "city": loc.get("City"),
                "country": loc.get("Country"),
            }
            for loc in (locations if isinstance(locations, list) else [])
        ],
    }


def _fetch_extended_properties(stock_item_id: str) -> list:
    rows = call_linnworks(
        "Inventory/GetInventoryItemExtendedProperties",
        {"inventoryItemId": stock_item_id},
    )
    return rows if isinstance(rows, list) else []


@_tool()
def get_extended_properties(sku: str) -> dict:
    """
    Fetch the extended properties (custom metadata) for an inventory item by SKU.

    Extended properties are key-value pairs attached to a product in Linnworks,
    used to store custom attributes like supplier codes, dimensions, materials,
    or any other product-level metadata. Useful for questions like "what are the
    extended properties for SKU ABC-123?" or "which products have a supplier code
    of XYZ?".

    Args:
        sku: The exact SKU / item number to look up.

    Returns:
        A dict with the item identity and a list of extended property records,
        each with a name, value, type, and row_id (the pkRowId — needed by
        delete_extended_properties to remove a specific row).
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    props = _fetch_extended_properties(stock_item_id)

    return {
        "sku": sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "count": len(props),
        "extended_properties": [
            {
                "name": p.get("ProperyName"),
                "value": p.get("PropertyValue"),
                "type": p.get("PropertyType"),
                "row_id": p.get("pkRowId"),
            }
            for p in props
        ],
    }


def _flatten_order_item(i: dict) -> dict:
    sub = i.get("CompositeSubItems") or []
    return {
        "sku": i.get("SKU"),
        "title": i.get("Title"),
        "quantity": i.get("Quantity"),
        "price_per_unit": i.get("PricePerUnit"),
        "line_total_ex_tax": i.get("Cost"),
        "line_total_inc_tax": i.get("CostIncTax"),
        "tax": i.get("Tax"),
        "tax_rate": i.get("TaxRate"),
        "category": i.get("CategoryName"),
        "stock_item_id": i.get("StockItemId") or i.get("ItemId"),
        "bin_rack": i.get("BinRack"),
        "channel_sku": i.get("ChannelSKU"),
        "channel_line_id": i.get("ItemNumber"),
        "channel_line_source": i.get("ItemSource"),
        "added_date": i.get("AddedDate"),
        "is_unlinked": i.get("IsUnlinked"),
        "composite_sub_items": [_flatten_order_item(s) for s in sub],
    }


def _batch_order_items(order_guids: list[str]) -> dict[str, list]:
    result: dict[str, list] = {}
    batch_size = 50
    for i in range(0, len(order_guids), batch_size):
        batch = order_guids[i : i + batch_size]
        detail_orders = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        if not isinstance(detail_orders, list):
            detail_orders = detail_orders.get("Orders") or []
        for order in detail_orders:
            oid = order.get("OrderId")
            items = order.get("Items") or []
            result[oid] = [_flatten_order_item(i) for i in items]
    return result


def _fetch_supplier_for_items(stock_item_ids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    batch_size = 200
    for i in range(0, len(stock_item_ids), batch_size):
        batch = stock_item_ids[i : i + batch_size]
        try:
            resp = call_linnworks(
                "Stock/GetStockItemsFullByIds",
                {"request": {"StockItemIds": batch, "DataRequirements": [1]}},
            )
            for item in resp.get("StockItemsFullExtended", []):
                iid = item.get("StockItemId", "")
                suppliers = item.get("Suppliers") or []
                if suppliers:
                    sup = next(
                        (s for s in suppliers if s.get("IsDefault")), suppliers[0]
                    )
                    result[iid] = {
                        "supplier_name": sup.get("Supplier") or "No Supplier",
                        "supplier_id": sup.get("SupplierID") or "",
                    }
                else:
                    result[iid] = {"supplier_name": "No Supplier", "supplier_id": ""}
        except RateLimitError:
            raise
        except Exception:
            pass
    return result


@_tool()
def get_processed_order_items(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 500,
) -> dict:
    """
    List processed orders with full line-item detail within a date range.

    This is the primary tool for sales analysis: it returns each processed order
    along with every SKU, quantity, and price in that order. Use it to answer
    questions like "what were our top-selling SKUs last week?", "which items are
    commonly bought together?", "what was our revenue by product category?", or
    "how many units of SKU ABC-123 did we sell in May?".

    Internally fetches order summaries from ProcessedOrders/SearchProcessedOrders,
    then enriches with line items from Orders/GetOrdersById in batches.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-08".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".
        page: Page number for paginated results. Defaults to 1.
        page_size: Orders per page. Min 20, defaults to 500 (recommended for large
            date ranges to minimise round-trips). Each page triggers a batch detail
            fetch.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - page, total_pages, total_count:  pagination info
          - count:                           orders on this page
          - orders:                          list of orders, each with an `items` list
    """
    page_size = max(20, page_size)

    summary_response = call_linnworks(
        "ProcessedOrders/SearchProcessedOrders",
        {
            "request": {
                "DateField": date_field,
                "FromDate": f"{from_date}T00:00:00",
                "ToDate": f"{to_date}T23:59:59",
                "PageNumber": page,
                "ResultsPerPage": page_size,
            }
        },
    )
    wrapper = summary_response.get("ProcessedOrders") or {}
    raw_orders = wrapper.get("Data") or []
    total_count = wrapper.get("TotalEntries", 0)
    total_pages = wrapper.get("TotalPages", 1)

    if not raw_orders:
        return {
            "from_date": from_date, "to_date": to_date, "date_field": date_field,
            "page": page, "total_pages": total_pages, "total_count": total_count,
            "count": 0, "orders": [],
        }

    guids = [o["pkOrderID"] for o in raw_orders]
    items_by_order = _batch_order_items(guids)

    orders = []
    for o in raw_orders:
        guid = o.get("pkOrderID")
        orders.append({
            "order_id": guid,
            "num_order_id": o.get("nOrderId"),
            "reference_num": o.get("ReferenceNum"),
            "external_reference": o.get("ExternalReference"),
            "source": o.get("Source"),
            "sub_source": o.get("SubSource"),
            "received_date": o.get("dReceivedDate"),
            "processed_date": o.get("dProcessedOn"),
            "total_charge": o.get("fTotalCharge"),
            "currency": o.get("cCurrency"),
            "postal_service_name": o.get("PostalServiceName"),
            "tracking_number": o.get("PostalTrackingNumber"),
            "country": o.get("cCountry"),
            "fulfilment_location": o.get("FulfilmentLocationName"),
            "items": items_by_order.get(guid, []),
        })

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(orders),
        "orders": orders,
    }


_NON_CHANNEL_ORDER_SOURCES = {"", "DIRECT"}

_INTERNAL_SKUS: set[str] = set()

if _env("LINNWORKS_INTERNAL_SKUS"):
    _INTERNAL_SKUS = {
        s.strip().casefold() for s in _env("LINNWORKS_INTERNAL_SKUS").split(",") if s.strip()
    }


def _is_internal_sku(sku: str | None) -> bool:
    return bool(sku) and sku.strip().casefold() in _INTERNAL_SKUS


def _is_channel_order_source(source: str | None) -> bool:
    return bool(source) and source.strip().upper() not in _NON_CHANNEL_ORDER_SOURCES


def _classify_order_line(
    flat_item: dict,
    is_channel_order: bool,
    is_composite_child: bool = False,
) -> list[dict]:
    channel_line_id = flat_item.get("channel_line_id")
    channel_line_source = flat_item.get("channel_line_source")

    if is_composite_child:
        status, reason = "not_expected", "composite_child"
    elif not is_channel_order:
        status, reason = "not_expected", "manual_order"
    elif channel_line_id is None and channel_line_source is None:
        status, reason = "unknown", "fields_absent"
    elif not (channel_line_source or "").strip():
        if _is_internal_sku(flat_item.get("sku")):
            status, reason = "not_expected", "internal_sku"
        else:
            status, reason = "unlinked", None
    else:
        status, reason = "linked", None

    row = {
        "sku": flat_item.get("sku"),
        "title": flat_item.get("title"),
        "channel_line_id": channel_line_id,
        "channel_line_source": channel_line_source,
        "channel_sku": flat_item.get("channel_sku"),
        "is_composite_child": is_composite_child,
        "status": status,
        "reason": reason,
    }

    rows = [row]
    for sub in flat_item.get("composite_sub_items") or []:
        rows.extend(_classify_order_line(sub, is_channel_order, is_composite_child=True))
    return rows


@_tool()
def find_unlinked_order_lines(
    include_open: bool = True,
    from_date: str | None = None,
    to_date: str | None = None,
    date_field: str = "processed",
    location_id: str = DEFAULT_LOCATION_ID,
    only_problems: bool = True,
) -> dict:
    """
    Scan orders and classify every line as linked, unlinked, unknown or
    not_expected, by whether it still carries the channel-identity fields
    (ItemNumber, ItemSource, ChannelSKU) that a channel-downloaded line normally
    has. A channel order line without them can't be fulfilled on the storefront
    when the order is despatched.

    This is a REPORT, not a fix: it writes nothing. To repair a line on an open
    order use relink_order_line; a line on an order already despatched can't be
    repaired. This tool does not assert how Linnworks internally matches a
    despatch to its storefront line; it reports the fields. It also can't see a
    link that points at the WRONG storefront line: a line reads as linked whenever
    ItemSource is non-blank.

    Scope: open orders (include_open), a processed-order date range (from_date
    and to_date), or both. Every order is read with Orders/GetOrdersById.

    Statuses:
      - linked: the line carries its channel reference.
      - unlinked: a channel order line whose channel reference is blank — the
        fault this looks for.
      - not_expected (not counted as unlinked): a composite child, a line on a
        non-channel order (e.g. "DIRECT"), or an internal marker/service SKU
        (Check-notes, APPLYGRIP, royal-mail-placement, …; reason `internal_sku`).
        internal_sku only applies where the line would otherwise be unlinked, and
        it is decided by SKU alone, so it is coarse: a marker SKU that is also
        sold as a product has its genuine orphans hidden too. Every such line is
        counted in `internal_sku_suppressed`.
      - unknown (not counted as unlinked): the API never returned the fields at
        all, so a scan never reports "0 unlinked" on fields it didn't see.

    A line deliberately added in Linnworks after the order downloaded also reads
    as unlinked. A timing rule on AddedDate/IsUnlinked was investigated and not built: on those fields a deliberate add and a genuine
    orphan cannot be told apart. The raw added_date and is_unlinked are returned by
    get_processed_order_items for manual review; this tool ignores them.

    Coverage is reported, not assumed: orders throttled by the rate limit go to
    `rate_limited_orders`, orders GetOrdersById didn't return go to
    `missing_orders`, and either sets `complete` to False. Retry those before
    trusting a clean result.

    Args:
        include_open: Include open (unprocessed) orders. Default True.
        from_date: Start of a processed-order date range, ISO (e.g. "2026-05-01").
            Needs to_date too.
        to_date: End of the processed-order date range, ISO.
        date_field: Processed-order date to filter on: "received", "processed"
            (default), "payment" or "cancelled".
        location_id: Location to scan for open orders. Defaults to "Default".
        only_problems: Default True: `orders` lists only orders with an unlinked
            or unknown line. `counts` always cover every line scanned.

    Returns:
        A dict with the query parameters; order_count_scanned (orders actually
        read and classified, not GUIDs requested); counts {linked, unlinked,
        unknown, not_expected}; internal_sku_suppressed; orders (order_id,
        num_order_id, reference_num, source, processed, lines with sku, title,
        channel_line_id, channel_line_source, channel_sku, is_composite_child,
        status, reason); rate_limited_orders; missing_orders; complete.
    """
    if not include_open and not (from_date and to_date):
        raise ValueError(
            "Nothing to scan: set include_open=True and/or provide both "
            "from_date and to_date for a processed-order date range."
        )

    order_meta: dict[str, dict] = {}

    if include_open:
        resp = call_linnworks(
            "OpenOrders/GetOrdersLowFidelity", {"request": {"LocationId": location_id}}
        )
        for o in resp.get("Orders") or []:
            guid = o.get("pkOrderID")
            if guid:
                order_meta[guid] = {
                    "processed": False,
                    "reference_num": o.get("ReferenceNum"),
                    "num_order_id": o.get("OrderId"),
                }

    if from_date and to_date:
        page = 1
        total_pages: int | None = None
        while total_pages is None or page <= total_pages:
            resp = call_linnworks(
                "ProcessedOrders/SearchProcessedOrders",
                {
                    "request": {
                        "DateField": date_field,
                        "FromDate": f"{from_date}T00:00:00",
                        "ToDate": f"{to_date}T23:59:59",
                        "PageNumber": page,
                        "ResultsPerPage": 500,
                    }
                },
            )
            wrapper = resp.get("ProcessedOrders") or {}
            raw_orders = wrapper.get("Data") or []
            if total_pages is None:
                total_pages = wrapper.get("TotalPages", 1)
            if not raw_orders:
                break
            for o in raw_orders:
                guid = o.get("pkOrderID")
                if guid:
                    order_meta[guid] = {
                        "processed": True,
                        "reference_num": o.get("ReferenceNum"),
                        "num_order_id": o.get("nOrderId"),
                    }
            page += 1

    guids = list(order_meta.keys())
    counts = {"linked": 0, "unlinked": 0, "unknown": 0, "not_expected": 0}
    internal_sku_suppressed = 0
    order_rows: list[dict] = []
    rate_limited_orders: list[dict] = []
    missing_orders: list[dict] = []
    classified_guids: set[str] = set()

    batch_size = 50
    for i in range(0, len(guids), batch_size):
        batch = guids[i : i + batch_size]
        try:
            detail_orders = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        except RateLimitError:
            for g in batch:
                rate_limited_orders.append({"order_id": g, **order_meta.get(g, {})})
            continue

        if not isinstance(detail_orders, list):
            detail_orders = detail_orders.get("Orders") or []

        returned_guids_lower: set[str] = set()

        for order in detail_orders:
            guid = order.get("OrderId")
            guid_lower = guid.lower() if guid else None
            if guid_lower:
                returned_guids_lower.add(guid_lower)
                if guid_lower in classified_guids:
                    continue
                classified_guids.add(guid_lower)

            general = order.get("GeneralInfo") or {}
            source = general.get("Source")
            is_channel = _is_channel_order_source(source)

            lines: list[dict] = []
            for raw_item in order.get("Items") or []:
                flat = _flatten_order_item(raw_item)
                lines.extend(_classify_order_line(flat, is_channel))

            for line in lines:
                counts[line["status"]] += 1
                if line["reason"] == "internal_sku":
                    internal_sku_suppressed += 1

            order_rows.append({
                "order_id": guid,
                "num_order_id": order.get("NumOrderId"),
                "reference_num": general.get("ReferenceNum"),
                "source": source,
                "processed": bool(order.get("Processed")),
                "lines": lines,
            })

        for g in batch:
            if g.lower() not in returned_guids_lower:
                missing_orders.append({"order_id": g, **order_meta.get(g, {})})

    order_count_scanned = len(order_rows)

    if only_problems:
        order_rows = [
            o for o in order_rows
            if any(l["status"] in ("unlinked", "unknown") for l in o["lines"])
        ]

    return {
        "include_open": include_open,
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "only_problems": only_problems,
        "order_count_scanned": order_count_scanned,
        "counts": counts,
        "internal_sku_suppressed": internal_sku_suppressed,
        "orders": order_rows,
        "rate_limited_orders": rate_limited_orders,
        "missing_orders": missing_orders,
        "complete": not rate_limited_orders and not missing_orders,
    }


_PICK_WAVE_STATE_LABELS: dict[str, str] = {
    "Unallocated": "Unallocated",
    "Allocated": "Allocated",
    "InProgress": "In Progress",
    "Abandoned": "Abandoned",
    "Shipped": "Shipped",
    "Complete": "Complete",
}

_BIN_TRACKING_UNAVAILABLE_MARKERS = (
    "non wms location",
    "non batched items",
)


def _pick_wave_state_label(raw_state: str | None) -> str:
    if raw_state in _PICK_WAVE_STATE_LABELS:
        return _PICK_WAVE_STATE_LABELS[raw_state]
    return f"unknown (unconfirmed raw state: {raw_state!r})"


def _format_pick_wave(row: dict) -> dict:
    raw_state = row.get("State")
    return {
        "picking_wave_id": row.get("PickingWaveId"),
        "state": raw_state,
        "state_label": _pick_wave_state_label(raw_state),
        "state_confirmed": raw_state in _PICK_WAVE_STATE_LABELS,
        "location_id": row.get("LocationId"),
        "user_id": row.get("UserId"),
        "email_address": row.get("EmailAddress"),
        "created_date": row.get("CreatedDate"),
        "start_time": row.get("StartTime"),
        "end_time": row.get("EndTime"),
        "order_count": row.get("OrderCount"),
        "item_count": row.get("ItemCount"),
        "items_picked": row.get("ItemsPicked"),
        "orders_picked": row.get("OrdersPicked"),
        "accumulated_in_progress_seconds": row.get("AccumulatedInProgressSeconds"),
        "group_type": row.get("GroupType"),
        "sort_type": row.get("SortType") or row.get("SortingType"),
    }


def _format_bin_row(row: dict) -> dict:
    return {
        "bin_rack": row.get("BinRack"),
        "bin_rack_id": row.get("BinRackId"),
        "bin_rack_type_name": row.get("BinrackTypeName"),
        "quantity": row.get("Quantity"),
        "in_transit": row.get("InTransit"),
        "picked_quantity": row.get("PickedQuantity"),
        "current_full_percentage": row.get("CurrentFullPercentage"),
        "batch_status": row.get("BatchStatus"),
        "batch_number": row.get("BatchNumber"),
        "expires_on": row.get("ExpiresOn"),
        "sell_by": row.get("SellBy"),
        "priority_sequence": row.get("PrioritySequence"),
        "location_id": row.get("LocationId"),
        "stock_item_id": row.get("StockItemId"),
    }


def _pick_wave_query_params(
    state: str | None, location_id: str | None, detail_level: str | None
) -> dict:
    params: dict = {}
    if state:
        params["state"] = state
    if location_id:
        params["locationId"] = location_id
    if detail_level:
        params["detailLevel"] = detail_level
    if not params:
        params["locationId"] = DEFAULT_LOCATION_ID
    return params


_PICK_WAVE_STATE_FILTER_NOTE = (
    "Omitting `state` does NOT mean 'all states', despite what the Linnworks "
    "API documents. Live-confirmed: with no state filter, a location with "
    "3,281 Shipped waves and 238 Abandoned waves returns 0 rows. Pass an "
    "explicit state ('Unallocated', 'Allocated', 'InProgress', 'Paused', "
    "'Complete', 'Abandoned', 'Packing', or 'Shipped') to see historical "
    "(Abandoned/Shipped) waves."
)


@_tool()
def get_pick_waves(
    state: str | None = None,
    location_id: str | None = None,
    detail_level: str | None = None,
) -> dict:
    """
    List pickwaves (Picking/GetAllPickingWaveHeaders). Read-only, and a
    point-in-time snapshot: orders leave waves and states move on while pickers
    work.

    ⚠️ Omitting `state` does NOT return all states, whatever the Linnworks docs
    say: it returned 0 waves where state="Shipped" returned thousands. Pass a
    state explicitly; there is no single call for "everything".

    `detail_level` doesn't change which waves come back or their fields here,
    only their order.

    Linnworks exposes no endpoint for adding an order to an existing pickwave.
    To change membership, use remove_orders_from_pick_waves or create new waves
    with generate_pick_waves. Use get_pick_wave_detail for a wave's orders and
    items, which these header rows don't carry.

    No weight or volume total is returned (the payload has none, and bundle
    parents weigh zero, so a sum would understate a trolley's load).

    Args:
        state: "Unallocated", "Allocated", "InProgress", "Paused", "Complete",
            "Abandoned", "Packing" or "Shipped".
        location_id: Location GUID. Defaults to Default only when no other
            filter is given (the API rejects an empty request).
        detail_level: "All" or "OnlyPickWave".

    Returns:
        state_filter, location_id_filter, detail_level_filter, state_filter_note,
        count, waves (each with `state` verbatim and `state_label`, which is an
        explicit "unknown (unconfirmed raw state: ...)" marker for any state not
        yet confirmed on a real wave), and on throttling: rate_limited, error and
        complete=False (never reported as "0 waves").
    """
    params = _pick_wave_query_params(state, location_id, detail_level)

    try:
        resp = call_linnworks_get("Picking/GetAllPickingWaveHeaders", params=params)
    except RateLimitError as exc:
        return {
            "state_filter": state,
            "location_id_filter": location_id,
            "detail_level_filter": detail_level,
            "state_filter_note": _PICK_WAVE_STATE_FILTER_NOTE,
            "count": 0,
            "waves": [],
            "rate_limited": True,
            "error": str(exc),
            "complete": False,
        }

    rows = resp.get("PickwaveHeaders") or [] if isinstance(resp, dict) else []
    waves = [_format_pick_wave(r) for r in rows]

    return {
        "state_filter": state,
        "location_id_filter": location_id,
        "detail_level_filter": detail_level,
        "state_filter_note": _PICK_WAVE_STATE_FILTER_NOTE,
        "count": len(waves),
        "waves": waves,
        "rate_limited": False,
        "complete": True,
    }


@_tool()
def get_pick_wave_users(
    state: str | None = None,
    location_id: str | None = None,
    detail_level: str | None = None,
) -> dict:
    """
    List Linnworks warehouse users (pickers) with their current pickwave
    summary (Picking/GetPickwaveUsersWithSummary).

    Read-only. Same point-in-time snapshot caveat as get_pick_waves: this
    reflects wave assignment at the moment of the call, and there is no
    endpoint to add an order to an existing wave (see get_pick_waves'
    docstring for the full finding).

    ⚠️  `state` behaves DIFFERENTLY here than on get_pick_waves, and this is
    live-confirmed but genuinely ambiguous. Every one of the six non-terminal
    state values ("Unallocated", "Allocated", "InProgress", "Paused",
    "Complete", "Packing") returned the IDENTICAL 15-row result — one row per
    registered picker, each showing PickingWaveId 0 and State "Unallocated"
    regardless of which state was requested. state="Abandoned" and
    state="Shipped" DID return genuinely different historical counts (247 vs
    3,287 in the account tested), so the filter is not simply ignored outright.
    Because no wave was actually in a non-terminal state anywhere in the
    account tested, it cannot be told apart from "the filter has no
    effect for non-terminal states" — treat any non-terminal `state` value
    passed here as unverified until a real wave is checked against it.

    Like get_pick_waves, `state` omitted does not mean "all states" (see that
    tool's docstring), and no summed weight/volume figure is ever computed or
    returned here.

    Args:
        state: Same enum as get_pick_waves. See the warning above about
            non-terminal values on this specific endpoint.
        location_id: Filter to one Linnworks location GUID. Defaults to
            DEFAULT_LOCATION_ID only when no other filter is supplied.
        detail_level: "All" or "OnlyPickWave". Not independently verified on
            this endpoint (see get_pick_waves for the finding on its sibling
            endpoint, where it did not change the result set but did change
            the row order).

    Returns:
        A dict with:
          - state_filter, location_id_filter, detail_level_filter
          - state_filter_note: the "omitted state != all states" warning
          - non_terminal_state_warning: the ambiguity warning above, always
            present, so a caller can't miss it by only reading the docstring
          - count: number of rows returned
          - users: list of formatted rows (see _format_pick_wave) — one row
            per picker (idle or currently on a wave) for non-terminal states,
            or one row per historical wave for Abandoned/Shipped
          - rate_limited: True if the call was throttled after Linnworks'
            full retry ladder
          - error: present only when rate_limited is True
          - complete: False only when rate_limited is True
    """
    params = _pick_wave_query_params(state, location_id, detail_level)

    non_terminal_note = (
        "Live-confirmed: every non-terminal state value (Unallocated, "
        "Allocated, InProgress, Paused, Complete, Packing) returned the "
        "identical 15-row idle-picker roster in this account, because nothing "
        "was actively being picked during this build. Whether `state` "
        "actually discriminates among non-terminal values with a real wave "
        "present is unverified."
    )

    try:
        resp = call_linnworks_get("Picking/GetPickwaveUsersWithSummary", params=params)
    except RateLimitError as exc:
        return {
            "state_filter": state,
            "location_id_filter": location_id,
            "detail_level_filter": detail_level,
            "state_filter_note": _PICK_WAVE_STATE_FILTER_NOTE,
            "non_terminal_state_warning": non_terminal_note,
            "count": 0,
            "users": [],
            "rate_limited": True,
            "error": str(exc),
            "complete": False,
        }

    rows = resp.get("PickingWaves") or [] if isinstance(resp, dict) else []
    users = [_format_pick_wave(r) for r in rows]

    return {
        "state_filter": state,
        "location_id_filter": location_id,
        "detail_level_filter": detail_level,
        "state_filter_note": _PICK_WAVE_STATE_FILTER_NOTE,
        "non_terminal_state_warning": non_terminal_note,
        "count": len(users),
        "users": users,
        "rate_limited": False,
        "complete": True,
    }


def _is_bin_tracking_unavailable_error(exc: RuntimeError) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _BIN_TRACKING_UNAVAILABLE_MARKERS)


@_tool()
def get_item_bins(
    skus: list[str],
    location_id: str = DEFAULT_LOCATION_ID,
    include_non_pick_locations: bool = False,
) -> dict:
    """
    Look up bin/binrack locations for one or more SKUs (Picking/GetItemBinracks).

    Read-only, one Linnworks call per SKU (no bulk endpoint exists for this).

    Distinguishes FOUR outcomes per SKU, never collapsing one into another
    (AC11 — "this item has no bin configured" must never read the same as
    "the lookup failed or was throttled"):

      bins_found              — real bin data returned; see `bins`.
      no_bins_configured      — the call succeeded (HTTP 200) but every one
                                 of PickableBins/NonPickableBins/
                                 AlternateLocations came back empty. This
                                 genuinely means "no bin recorded for this
                                 item here", confirmed by a clean response,
                                 not by an absence of data.
      bin_tracking_unavailable — Linnworks refused the lookup with the
                                 live-confirmed error "Alternate locations
                                 aren't available for non batched items or
                                 items in a non WMS location." On an account
                                 whose locations all read IsWarehouseManaged:
                                 False (check with get_locations), expect this
                                 outcome on every real item at every location — it answers
                                 "does this warehouse populate bins?" with a
                                 clear no, distinct from "this one item has no
                                 bin".
      lookup_failed            — any other error. Reported with the raw
                                 message; never silently folded into
                                 no_bins_configured.

    Plus, separately: sku_not_found (the SKU itself doesn't resolve) and
    rate_limited (the call was throttled after Linnworks' full retry ladder)
    — both are their own outcomes too, and an unresolvable SKU never sinks
    the rest of the batch.

    No summed weight/volume figure is ever computed here. `current_full_percentage`
    on a bin row is the BIN's own volumetric fill level, passed through
    verbatim — never the item's weight, and never summed across bins.

    Args:
        skus: SKUs to look up. Each is resolved and queried independently.
        location_id: Linnworks location GUID to check. Defaults to "Default".
        include_non_pick_locations: If True, also ask Linnworks to include
            binracks that cannot currently be selected to pick from.

    Returns:
        A dict with:
          - location_id, include_non_pick_locations: the filters used
          - count: number of SKUs requested
          - results: per-SKU rows, each with sku, stock_item_id (None if
            unresolved), outcome (one of the values above), bins (list of
            _format_bin_row dicts — PickableBins + NonPickableBins +
            AlternateLocations merged, each tagged with a `bin_category`
            field; empty unless outcome is "bins_found"), and reason (the
            raw error text, present for bin_tracking_unavailable and
            lookup_failed only)
          - complete: False if any SKU hit rate_limited
    """
    cache: dict = {}
    results: list[dict] = []
    any_rate_limited = False

    for sku in skus:
        row: dict = {"sku": sku, "stock_item_id": None, "bins": []}
        try:
            stock_item_id = _resolve_sku_to_id(sku, cache)
        except RateLimitError as exc:
            row["outcome"] = "rate_limited"
            row["reason"] = str(exc)
            any_rate_limited = True
            results.append(row)
            continue
        except ValueError as exc:
            row["outcome"] = "sku_not_found"
            row["reason"] = str(exc)
            results.append(row)
            continue

        row["stock_item_id"] = stock_item_id

        params = {
            "stockItemId": stock_item_id,
            "stockLocationId": location_id,
            "includeNonPickLocations": include_non_pick_locations,
        }
        try:
            resp = call_linnworks_get("Picking/GetItemBinracks", params=params)
        except RateLimitError as exc:
            row["outcome"] = "rate_limited"
            row["reason"] = str(exc)
            any_rate_limited = True
            results.append(row)
            continue
        except RuntimeError as exc:
            if _is_bin_tracking_unavailable_error(exc):
                row["outcome"] = "bin_tracking_unavailable"
            else:
                row["outcome"] = "lookup_failed"
            row["reason"] = str(exc)
            results.append(row)
            continue

        pickable = [dict(_format_bin_row(b), bin_category="pickable") for b in (resp.get("PickableBins") or [])]
        non_pickable = [dict(_format_bin_row(b), bin_category="non_pickable") for b in (resp.get("NonPickableBins") or [])]
        alternate = [dict(_format_bin_row(b), bin_category="alternate_location") for b in (resp.get("AlternateLocations") or [])]
        bins = pickable + non_pickable + alternate

        row["bins"] = bins
        row["outcome"] = "bins_found" if bins else "no_bins_configured"
        results.append(row)

    return {
        "location_id": location_id,
        "include_non_pick_locations": include_non_pick_locations,
        "count": len(skus),
        "results": results,
        "complete": not any_rate_limited,
    }


@_tool()
def check_orders_pickable(order_ids: list[str]) -> dict:
    """
    Check whether orders are allocatable to a pickwave, without creating or
    changing anything (Picking/CheckAllocatableToPickwave).

    Read-only — proven side-effect free during this build (AC8): two
    consecutive identical calls returned byte-identical results; the
    account's Unallocated/Shipped wave counts (via get_pick_waves) were
    identical before and after; the checked orders' Processed/Status/
    IsParked fields were unchanged; and a stock level read
    (Stock/GetStockLevel_Batch) for one of the checked orders' SKUs was
    byte-identical before and after. This is a genuine check, not a hidden
    write — no dry_run parameter is offered because there is nothing to
    stage.

    Accepts both GUID and numeric order ids (same convention as every other
    order tool here — _resolve_order_guid). An id that cannot be resolved is
    reported as its own row in `resolve_errors` rather than raising and
    sinking the whole batch (AC9); the remaining, resolvable ids are still
    checked together in one call.

    Linnworks itself also reports a per-order "doesn't exist" error inside a
    successful response for a numeric id it doesn't recognise (live-confirmed:
    `HasErrors: true`, `Errors: [{"Error": "Order doesn't exist", ...}]`) —
    that is surfaced too, on `has_errors`/`errors`, separately from this
    tool's own `resolve_errors` bucket (which only covers ids this tool could
    not resolve at all before ever calling Linnworks).

    Args:
        order_ids: GUIDs or numeric order numbers, in any mix.

    Returns:
        A dict with:
          - count: number of order_ids requested
          - results: per-order rows for every id that resolved, each with
            order_id (as supplied), num_order_id, order_guid, pickable
            (True when Linnworks reports no errors for this order),
            has_errors, errors (Linnworks' own per-order error list, verbatim),
            and items (the per-line SKU/binrack/quantity detail Linnworks
            returns, passed through verbatim — no total is computed from it)
          - resolve_errors: ids that could not be resolved to a real order at
            all (bad GUID, unknown numeric id) — order_id and reason
          - rate_limited: ids whose resolution or check was throttled after
            Linnworks' full retry ladder — order_id and reason
          - complete: False if resolve_errors or rate_limited is non-empty
    """
    resolved, resolve_errors, rate_limited = _resolve_order_numbers(order_ids)

    results: list[dict] = []

    if resolved:
        num_ids = [r[1] for r in resolved]
        try:
            resp = call_linnworks(
                "Picking/CheckAllocatableToPickwave", {"request": {"OrderIds": num_ids}}
            )
        except RateLimitError as exc:
            for order_id, num_order_id, order_guid in resolved:
                rate_limited.append({
                    "order_id": order_id, "num_order_id": num_order_id, "reason": str(exc),
                })
            resp = {"Results": []}

        by_num_id = {r.get("OrderId"): r for r in (resp.get("Results") or [])}
        already_rate_limited_ids = {r["num_order_id"] for r in rate_limited if "num_order_id" in r}

        for order_id, num_order_id, order_guid in resolved:
            check = by_num_id.get(num_order_id)
            if check is None:
                if num_order_id in already_rate_limited_ids:
                    continue
                resolve_errors.append({
                    "order_id": order_id,
                    "reason": (
                        f"Order {num_order_id} was requested from "
                        "Picking/CheckAllocatableToPickwave but did not come "
                        "back in its response."
                    ),
                })
                continue
            errors = check.get("Errors") or []
            results.append({
                "order_id": order_id,
                "num_order_id": num_order_id,
                "order_guid": order_guid,
                "pickable": not check.get("HasErrors"),
                "has_errors": bool(check.get("HasErrors")),
                "errors": errors,
                "items": check.get("OrderDetails") or [],
            })

    return {
        "count": len(order_ids),
        "results": results,
        "resolve_errors": resolve_errors,
        "rate_limited": rate_limited,
        "complete": not resolve_errors and not rate_limited,
    }


_PICK_WAVE_EMPTY_DETAIL_NOTE = (
    "Linnworks returned no wave for this id. It does this for a SHIPPED wave, "
    "for an EMPTIED wave (removing a wave's last order auto-abandons it) — "
    "both confirmed live 22 Sep 2026 — or for an id that doesn't exist. It is "
    "NOT every FINISHED wave: a COMPLETE wave IS returned in full (confirmed "
    "live 23 Sep 2026, wave 3556, the same day it finished), as is an "
    "ABANDONED wave that still holds orders. A COMPLETE wave is readable ONLY "
    "WHILE COMPLETE: once it moves on to Shipped it returns nothing here "
    "(proven 23 Sep 2026 on waves 3554-3556). An empty response here "
    "does NOT mean the wave has no orders. Use get_pick_waves(state='Shipped' "
    "or 'Abandoned') to see a finished wave's header counts."
)

_PICK_WAVE_BLOCKER_FLAGS = (
    ("locked", "IsLocked"),
    ("on_hold", "IsOnHold"),
    ("cancelled", "IsCancelled"),
    ("processed", "IsProcessed"),
)


def _fetch_pick_wave(picking_wave_id: int) -> dict:
    resp = call_linnworks_get("Picking/GetPickingWave", {"pickingWaveId": picking_wave_id})
    return resp if isinstance(resp, dict) else {}


def _format_pick_wave_detail(resp: dict, picking_wave_id: int | None = None) -> dict | None:
    waves = resp.get("PickingWaves") or []
    if picking_wave_id is not None:
        target = _as_int(picking_wave_id)
        waves = [w for w in waves
                 if isinstance(w, dict) and _as_int(w.get("PickingWaveId")) == target]
    if not waves:
        return None
    wave = waves[0]

    skus = {str(s.get("StockItemId") or "").lower(): s for s in (resp.get("Skus") or [])}
    bins: dict[str, list[str]] = {}
    for b in resp.get("Bins") or []:
        sid = str(b.get("StockItemId") or "").lower()
        rack = b.get("BinRack")
        if rack and rack not in bins.setdefault(sid, []):
            bins[sid].append(rack)

    orders: list[dict] = []
    blockers: list[dict] = []
    for o in wave.get("Orders") or []:
        items = []
        for it in o.get("Items") or []:
            sid = str(it.get("StockItemId") or "").lower()
            sku = skus.get(sid, {})
            items.append({
                "picking_wave_item_row_id": it.get("PickingWaveItemsRowId"),
                "order_item_row_id": it.get("OrderItemRowId"),
                "stock_item_id": it.get("StockItemId"),
                "sku": sku.get("SKU"),
                "title": sku.get("ItemTitle"),
                "to_pick": it.get("ToPickQuantity"),
                "picked": it.get("PickedQuantity"),
                "item_state": it.get("ItemState"),
                "bins": bins.get(sid, []),
            })
        flags = {name: bool(o.get(key)) for name, key in _PICK_WAVE_BLOCKER_FLAGS}
        row = {
            "order_id": o.get("OrderId"),
            "order_guid": o.get("OrderId_Guid"),
            "pick_state": o.get("PickState"),
            "sort_order": o.get("SortOrder"),
            **{f"is_{name}": value for name, value in flags.items()},
            "is_paid": bool(o.get("IsPaid")),
            "items": items,
        }
        orders.append(row)
        reasons = [name for name, value in flags.items() if value]
        if reasons:
            blockers.append({
                "order_id": row["order_id"],
                "order_guid": row["order_guid"],
                "reasons": reasons,
            })

    return {**_format_pick_wave(wave), "orders": orders, "blockers": blockers}


@_tool()
def get_pick_wave_detail(picking_wave_id: int) -> dict:
    """
    Read one pickwave in full (Picking/GetPickingWave): its header, every
    order with its pick state and locked / on-hold / cancelled / processed /
    paid flags, and every item with SKU, title, quantity to pick, quantity
    picked and bin codes.

    Read-only. A point-in-time snapshot: pickers change waves while they work.

    `blockers` lists orders in the wave that are now locked, on hold,
    cancelled or processed — the candidates for remove_orders_from_pick_waves.

    ⚠️  Linnworks returns NOTHING for a SHIPPED wave or an EMPTIED wave
    (removing a wave's last order auto-abandons it — confirmed live 22 Sep
    2026), or for an unknown id. That comes back as found=False with a note,
    never as "this wave has no orders". An ABANDONED wave that still holds
    orders IS returned here. An unassigned wave has user_id None.

    Bin codes come from the wave's own Bins data. That works even on an
    account with no WMS-managed locations, where get_item_bins can't.

    Args:
        picking_wave_id: The wave's id, from get_pick_waves or generate_pick_waves.

    Returns:
        found (True / False, or None when rate limited). For a found wave:
        the same header fields as get_pick_waves (picking_wave_id, state,
        state_label, user_id, email_address, counts, group_type, sort_type)
        plus orders[] and blockers[]. Always: rate_limited, complete.
    """
    try:
        resp = _fetch_pick_wave(picking_wave_id)
    except RateLimitError as exc:
        return {
            "picking_wave_id": picking_wave_id,
            "found": None,
            "rate_limited": True,
            "complete": False,
            "error": str(exc),
        }
    detail = _format_pick_wave_detail(resp, picking_wave_id)
    if detail is None:
        return {
            "picking_wave_id": picking_wave_id,
            "found": False,
            "note": _PICK_WAVE_EMPTY_DETAIL_NOTE,
            "rate_limited": False,
            "complete": True,
        }
    return {**detail, "found": True, "rate_limited": False, "complete": True}


_PICK_WAVE_SETTABLE_STATES = ("Abandoned", "Paused", "Unallocated")
_PICK_WAVE_ABANDON_WARNING = (
    "Abandoning does NOT release its orders — Linnworks keeps them in the "
    "abandoned wave, so they can't go into a new wave until removed with "
    "remove_orders_from_pick_waves (live-confirmed 22 Sep 2026)."
)
_PICK_WAVE_OPEN_STATES = ("Unallocated", "Allocated", "InProgress", "Paused", "Complete", "Packing")
_PICK_WAVE_ALL_STATES = (
    "Unallocated", "Allocated", "InProgress", "Paused", "Complete", "Abandoned", "Packing", "Shipped",
)
_PICK_WAVE_STARTED_STATES = ("InProgress", "Paused", "Complete", "Packing")
_PICK_WAVE_SORTING_TYPES = ("BinPriority", "OrderView")
_PICK_WAVE_GROUP_TYPES = ("Items", "Orders")
_PICKWAVE_READY_TAG = (
    _env("LINNWORKS_PICKWAVE_READY_TAG") or ""
).upper()

_PICKING_WRITE_WRAPPED: dict[str, bool] = {
    "Picking/GeneratePickingWave": False,
    "Picking/UpdatePickingWaveHeader": False,
    "Picking/DeleteOrdersFromPickingWaves": True,
}


def _picking_write(path: str, body: dict) -> dict:
    payload = {"request": body} if _PICKING_WRITE_WRAPPED[path] else body
    return call_linnworks(path, payload)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_order_numbers(order_ids: list) -> tuple[list[tuple], list[dict], list[dict]]:
    resolved: list[tuple] = []
    resolve_errors: list[dict] = []
    rate_limited: list[dict] = []
    for order_id in order_ids:
        try:
            order_guid, raw = _resolve_order_guid(str(order_id))
        except RateLimitError as exc:
            rate_limited.append({"order_id": order_id, "reason": str(exc)})
            continue
        except RuntimeError as exc:
            resolve_errors.append({"order_id": order_id, "reason": str(exc)})
            continue
        num_order_id = raw.get("NumOrderId")
        if num_order_id is None:
            resolve_errors.append({
                "order_id": order_id,
                "reason": f"Order '{order_id}' resolved but carries no NumOrderId.",
            })
            continue
        resolved.append((order_id, num_order_id, order_guid))
    return resolved, resolve_errors, rate_limited


def _fetch_picker_roster() -> dict[int, str | None]:
    resp = call_linnworks_get(
        "Picking/GetPickwaveUsersWithSummary",
        _pick_wave_query_params("Unallocated", None, None),
    )
    roster: dict[int, str | None] = {}
    for row in (resp.get("PickingWaves") or []) if isinstance(resp, dict) else []:
        user_id = row.get("UserId")
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            roster.setdefault(user_id, row.get("EmailAddress"))
    return roster


def _fetch_fifo_ready_guids(order_guids: list[str]) -> set[str]:
    ready: set[str] = set()
    for start in range(0, len(order_guids), 100):
        chunk = order_guids[start:start + 100]
        resp = call_linnworks("OpenOrders/GetIdentifiersByOrderIds", {"OrderIds": chunk})
        for row in resp if isinstance(resp, list) else []:
            if str(row.get("Tag", "")).strip().upper() == _PICKWAVE_READY_TAG:
                ready.add(str(row.get("fkOrderId", "")).lower())
    return ready


def _check_pickable_numbers(num_ids: list[int]) -> dict[int, dict]:
    if not num_ids:
        return {}
    resp = call_linnworks(
        "Picking/CheckAllocatableToPickwave", {"request": {"OrderIds": num_ids}}
    )
    return {
        r.get("OrderId"): {"pickable": not r.get("HasErrors"), "errors": r.get("Errors") or []}
        for r in (resp.get("Results") or [])
    }


def _find_wave_header(
    picking_wave_id: int, state: str, location_id: str = DEFAULT_LOCATION_ID
) -> dict | None:
    resp = call_linnworks_get(
        "Picking/GetAllPickingWaveHeaders", {"state": state, "locationId": location_id}
    )
    for row in (resp.get("PickwaveHeaders") or []) if isinstance(resp, dict) else []:
        if row.get("PickingWaveId") == picking_wave_id:
            return {**_format_pick_wave(row), "user_key_present": "UserId" in row}
    return None


def _pick_wave_preflight_throttled(rate_limited: list[dict]) -> dict:
    return {
        "success": False,
        "rate_limited": rate_limited,
        "complete": False,
        "message": (
            "Linnworks' rate limit was hit during the checks before writing. "
            "Nothing was written. Wait a minute and call again."
        ),
    }


def _pick_wave_unknown_user(user_id, roster: dict) -> dict:
    known = ", ".join(f"{uid} ({email})" for uid, email in sorted(roster.items()))
    return {
        "success": False,
        "error": f"user_id {user_id} is not a Linnworks picker. Known pickers: {known}",
    }


def _read_back_generated_wave(wave_ids: list[int], requested: list[int]) -> dict:
    got: list[int] = []
    try:
        for wave_id in wave_ids:
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id), wave_id)
            if detail is None:
                return {
                    "outcome": "unconfirmed",
                    "picking_wave_ids": wave_ids,
                    "readback_error": f"wave {wave_id} was reported created but reads back empty",
                }
            got.extend(o["order_id"] for o in detail["orders"])
    except RateLimitError as exc:
        return {"outcome": "unconfirmed", "picking_wave_ids": wave_ids,
                "readback_error": f"rate limited: {exc}"}
    except RuntimeError as exc:
        return {"outcome": "unconfirmed", "picking_wave_ids": wave_ids,
                "readback_error": str(exc)}
    except Exception as exc:
        return {"outcome": "unconfirmed", "picking_wave_ids": wave_ids,
                "readback_error": f"{type(exc).__name__}: {exc}"}
    return {
        "outcome": "created",
        "picking_wave_ids": wave_ids,
        "readback_order_ids": sorted(got),
        "readback_matches": sorted(got) == sorted(requested),
        "missing_order_ids": sorted(set(requested) - set(got)),
        "extra_order_ids": sorted(set(got) - set(requested)),
    }


@_tool()
def generate_pick_waves(
    waves: list[dict],
    location_id: str = DEFAULT_LOCATION_ID,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create one or more pickwaves (Picking/GeneratePickingWave), one Linnworks
    call per wave.

    Each wave is a dict:
        {"order_ids": [...],            # GUIDs or order numbers, in pick order
         "user_id": 19,                 # optional — see get_pick_wave_users; omit for unassigned
         "sorting_type": "BinPriority", # or "OrderView" (default BinPriority)
         "group_type": "Items"}         # or "Orders" (default Items)

    Before anything is written — on a dry run too — every order is resolved;
    an order may appear only once per call; every user_id must be on the live
    picker roster; Linnworks' own pickability check runs for every order (an
    order already in a wave, locked, parked etc. is flagged with Linnworks'
    reason); and, when LINNWORKS_PICKWAVE_READY_TAG is set, orders without that
    identifier get a WARNING (not a block); each order's `fifo_ready` is None
    when the check is off. An unresolved order blocks its own wave only. A rate limit during
    these checks stops the call with nothing written.

    Staged above 25 orders across all waves (confirmed_count). dry_run=True
    by default.

    Every created wave is read back with a fresh GetPickingWave call. A wave
    whose read-back fails is `unconfirmed` — it may exist; check with
    get_pick_waves before trying again. A wave is `refused` only when
    Linnworks creates nothing AND gives a reason (ValidationResults). A
    response with neither a wave nor a reason, or a timeout / dropped
    connection after sending, is also `unconfirmed`, because the wave may
    exist.

    ⚠️  Not atomic across waves. If a multi-wave run partly fails, the result
    lists exactly which waves were created. Re-send only the failed waves —
    re-running the whole batch would put the created waves' orders through
    generate again.

    There is no endpoint to add an order to an existing wave: remove it and
    generate again. A wave is a point-in-time snapshot while pickers work.

    ⚠️  The body is sent UNWRAPPED until the live proof settles it
    (_PICKING_WRITE_WRAPPED).

    Returns:
        A refusal before any write: success False, error (or, for a rate
        limit during the checks: success False, rate_limited, complete False,
        message).
        Staged (more than 25 orders, no matching confirmed_count): staged,
        success, item_count, threshold, confirmed_count, message, manifest,
        warnings.
        Dry run: dry_run True, wave_count, order_count, manifest (per wave:
        wave_index, user_id, user_email, sorting_type, group_type, orders
        [order_id, resolved, num_order_id, order_guid, pickable,
        pickable_errors, fifo_ready], blocked, blocked_reasons, warnings),
        warnings, message.
        Live: dry_run False, results, created_wave_ids, rate_limited,
        warnings, complete, message. Each result has wave_index, user_id,
        outcome and validation_results (Linnworks' ValidationResults
        verbatim, [] when absent — kept even on a created wave). By outcome:
          created: picking_wave_ids, readback_order_ids, readback_matches,
            missing_order_ids, extra_order_ids;
          unconfirmed: picking_wave_ids + readback_error (read-back failed),
            or note + raw_response (no wave and no reason), or note + error
            (network error after sending);
          refused: validation_results only;
          rate_limited / error: error;
          blocked: reasons.
    """
    if not waves:
        return {"success": False, "error": "waves is empty — pass at least one wave."}
    problems: list[str] = []
    for i, wave in enumerate(waves):
        if not isinstance(wave, dict) or not wave.get("order_ids"):
            problems.append(f"wave {i}: needs a non-empty order_ids list")
            continue
        if wave.get("sorting_type", "BinPriority") not in _PICK_WAVE_SORTING_TYPES:
            problems.append(f"wave {i}: sorting_type must be one of {list(_PICK_WAVE_SORTING_TYPES)}")
        if wave.get("group_type", "Items") not in _PICK_WAVE_GROUP_TYPES:
            problems.append(f"wave {i}: group_type must be one of {list(_PICK_WAVE_GROUP_TYPES)}")
        user_id = wave.get("user_id")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        ):
            problems.append(f"wave {i}: user_id must be a positive integer (see get_pick_wave_users)")
    if problems:
        return {"success": False, "error": "Invalid waves: " + "; ".join(problems)}

    seen_inputs: dict[str, int] = {}
    for i, wave in enumerate(waves):
        for order_id in wave["order_ids"]:
            key = str(order_id).strip().lower()
            if key in seen_inputs:
                return {"success": False, "error": (
                    f"Order {order_id!r} appears more than once (wave {seen_inputs[key]} "
                    f"and wave {i}). An order can only go in one wave per call.")}
            seen_inputs[key] = i

    all_ids = [order_id for wave in waves for order_id in wave["order_ids"]]

    resolved, resolve_errors, rate_limited = _resolve_order_numbers(all_ids)
    if rate_limited:
        return _pick_wave_preflight_throttled(rate_limited)
    by_input = {str(r[0]).strip().lower(): (r[1], r[2]) for r in resolved}

    seen_nums: dict[int, int] = {}
    for i, wave in enumerate(waves):
        for order_id in wave["order_ids"]:
            hit = by_input.get(str(order_id).strip().lower())
            if hit is None:
                continue
            num = hit[0]
            if num in seen_nums:
                return {"success": False, "error": (
                    f"Order {num} is listed twice (wave {seen_nums[num]} and wave {i}), "
                    "once by GUID and once by number. An order can only go in one wave per call.")}
            seen_nums[num] = i

    wanted_users = {w["user_id"] for w in waves if w.get("user_id") is not None}
    roster: dict[int, str | None] = {}
    if wanted_users:
        try:
            roster = _fetch_picker_roster()
        except RateLimitError as exc:
            return _pick_wave_preflight_throttled([{"step": "picker roster", "reason": str(exc)}])
        except RuntimeError as exc:
            return {"success": False,
                    "error": f"Picker roster lookup failed — nothing was written: {exc}"}
        unknown = sorted(u for u in wanted_users if u not in roster)
        if unknown:
            return _pick_wave_unknown_user(unknown[0] if len(unknown) == 1 else unknown, roster)

    warnings: list[str] = []
    try:
        pickability = _check_pickable_numbers([r[1] for r in resolved])
    except RateLimitError as exc:
        return _pick_wave_preflight_throttled([{"step": "pickability check", "reason": str(exc)}])
    except RuntimeError as exc:
        return {"success": False,
                "error": f"Pickability check failed — nothing was written: {exc}"}
    fifo_ready: set[str] | None = None
    if _PICKWAVE_READY_TAG:
        try:
            guids = [r[2] for r in resolved]
            fifo_ready = _fetch_fifo_ready_guids(guids) if guids else set()
        except RateLimitError as exc:
            return _pick_wave_preflight_throttled([{"step": "ready-tag check", "reason": str(exc)}])
        except RuntimeError as exc:
            fifo_ready = None
            warnings.append(f"{_PICKWAVE_READY_TAG} check was SKIPPED, not passed: {exc}")

    errors_by_input = {str(e["order_id"]).strip().lower(): e["reason"] for e in resolve_errors}
    manifest: list[dict] = []
    for i, wave in enumerate(waves):
        user_id = wave.get("user_id")
        orders: list[dict] = []
        blocked_reasons: list[str] = []
        wave_warnings: list[str] = []
        for order_id in wave["order_ids"]:
            key = str(order_id).strip().lower()
            if key in errors_by_input:
                blocked_reasons.append(f"order {order_id!r} could not be resolved: {errors_by_input[key]}")
                orders.append({"order_id": order_id, "resolved": False})
                continue
            num, guid = by_input[key]
            check = pickability.get(num)
            is_fifo = None if fifo_ready is None else guid.lower() in fifo_ready
            if check is not None and not check["pickable"]:
                wave_warnings.append(f"order {num}: Linnworks says it is not pickable: {check['errors']}")
            if is_fifo is False:
                wave_warnings.append(f"order {num} is not tagged {_PICKWAVE_READY_TAG}")
            orders.append({
                "order_id": order_id,
                "resolved": True,
                "num_order_id": num,
                "order_guid": guid,
                "pickable": None if check is None else check["pickable"],
                "pickable_errors": [] if check is None else check["errors"],
                "fifo_ready": is_fifo,
            })
        manifest.append({
            "wave_index": i,
            "user_id": user_id,
            "user_email": roster.get(user_id) if user_id is not None else None,
            "sorting_type": wave.get("sorting_type", "BinPriority"),
            "group_type": wave.get("group_type", "Items"),
            "orders": orders,
            "blocked": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "warnings": wave_warnings,
        })

    guard = _write_guard("generate_pick_waves", all_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "warnings": warnings}
    if dry_run:
        sendable = sum(not m["blocked"] for m in manifest)
        return {
            "dry_run": True,
            "wave_count": len(waves),
            "order_count": len(all_ids),
            "manifest": manifest,
            "warnings": warnings,
            "message": f"Dry run — {sendable} of {len(waves)} wave(s) would be sent. Nothing was created.",
        }

    results: list[dict] = []
    for m in manifest:
        base = {"wave_index": m["wave_index"], "user_id": m["user_id"], "validation_results": []}
        if m["blocked"]:
            results.append({**base, "outcome": "blocked", "reasons": m["blocked_reasons"]})
            continue
        nums = [o["num_order_id"] for o in m["orders"]]
        body = {
            "LocationId": location_id,
            "SortingType": m["sorting_type"],
            "GroupType": m["group_type"],
            "Orders": [{"OrderId": n, "SortOrder": pos} for pos, n in enumerate(nums)],
        }
        if m["user_id"] is not None:
            body["UserId"] = m["user_id"]
        try:
            resp = _picking_write("Picking/GeneratePickingWave", body)
        except RateLimitError as exc:
            results.append({**base, "outcome": "rate_limited", "error": str(exc)})
            continue
        except RuntimeError as exc:
            results.append({**base, "outcome": "error", "error": str(exc)})
            continue
        except Exception as exc:
            results.append({
                **base,
                "outcome": "unconfirmed",
                "error": f"{type(exc).__name__}: {exc}",
                "note": ("The request may have reached Linnworks and the wave may exist. "
                         "Check get_pick_waves(state='Unallocated') before re-sending this wave."),
            })
            continue
        if isinstance(resp, dict):
            validation = resp.get("ValidationResults") or []
            created = [w.get("PickingWaveId") for w in (resp.get("PickingWaves") or [])
                       if isinstance(w, dict) and w.get("PickingWaveId")]
        else:
            validation, created = [], []
        if created:
            results.append({**base, **_read_back_generated_wave(created, nums),
                            "validation_results": validation})
        elif validation:
            results.append({**base, "outcome": "refused", "validation_results": validation})
        else:
            results.append({
                **base,
                "outcome": "unconfirmed",
                "note": ("Linnworks' response neither created a wave nor gave a reason. Check "
                         "get_pick_waves(state='Unallocated') before re-sending this wave."),
                "raw_response": resp if isinstance(resp, dict) else repr(resp)[:500],
            })

    created_ids = [wid for r in results for wid in r.get("picking_wave_ids", [])]
    failed = [r["wave_index"] for r in results if r["outcome"] not in ("created", "unconfirmed")]
    uncertain = [r["wave_index"] for r in results
                 if r["outcome"] == "unconfirmed" and not r.get("picking_wave_ids")]
    complete = all(r["outcome"] == "created" and r.get("readback_matches") for r in results)
    message = f"{len(created_ids)} wave(s) created: {created_ids}."
    if sum(map(bool, (created_ids, failed, uncertain))) >= 2:
        message += " PARTIAL RUN:"
        if created_ids:
            message += f" waves {created_ids} already exist."
        message += " Do NOT re-run the whole batch"
        message += f" — re-send only wave index(es) {failed}." if failed else "."
    if uncertain:
        message += (f" UNCONFIRMED: wave index(es) {uncertain} may exist — check get_pick_waves "
                    "before re-sending.")
    for r in results:
        if r["outcome"] == "created" and not r.get("readback_matches"):
            ids = r["picking_wave_ids"]
            label = f"wave {ids[0]}" if len(ids) == 1 else f"waves {ids}"
            message += (f" Read-back mismatch: {label} does not match the request "
                        f"(missing {r['missing_order_ids']}, extra {r['extra_order_ids']}).")
    return {
        "dry_run": False,
        "results": results,
        "created_wave_ids": created_ids,
        "rate_limited": [r for r in results if r["outcome"] == "rate_limited"],
        "warnings": warnings,
        "complete": complete,
        "message": message,
    }


def _pick_wave_user(value) -> int | None:
    user = _as_int(value)
    return None if user is None or user in (0, -1) else user


def _read_back_updated_wave(
    picking_wave_id: int,
    target_state: str,
    expected_user: int | None,
    current_user: int | None,
    judge_state: bool,
    judge_user: bool,
    location_id: str,
) -> dict:
    try:
        if target_state == "Abandoned":
            after = _find_wave_header(picking_wave_id, "Abandoned", location_id)
        else:
            after = _format_pick_wave_detail(_fetch_pick_wave(picking_wave_id), picking_wave_id)
    except RateLimitError as exc:
        return {"outcome": "unconfirmed", "complete": False, "readback_error": f"rate limited: {exc}"}
    except RuntimeError as exc:
        return {"outcome": "unconfirmed", "complete": False, "readback_error": str(exc)}
    except Exception as exc:
        return {"outcome": "unconfirmed", "complete": False,
                "readback_error": f"{type(exc).__name__}: {exc}"}
    if after is None:
        return {"outcome": "unconfirmed", "complete": False,
                "readback_error": f"wave {picking_wave_id} could not be found after the update"}
    after_user = _pick_wave_user(after["user_id"])
    state_ok = after["state"] == target_state
    user_ok = after_user == _pick_wave_user(expected_user)
    applied = (state_ok or not judge_state) and (user_ok or not judge_user)
    out = {
        "outcome": "updated" if applied else "not_applied",
        "complete": applied,
        "after_state": after["state"],
        "after_user_id": after["user_id"],
        "after_email": after["email_address"],
    }
    if not judge_state:
        out["state_changed_by_server"] = not state_ok
    if not judge_user and after.get("user_key_present", True):
        out["user_changed_by_server"] = after_user != _pick_wave_user(current_user)
    return out


@_tool()
def update_pick_wave(
    picking_wave_id: int,
    user_id: int | None = None,
    unassign: bool = False,
    state: str | None = None,
    allow_in_progress: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Reassign, unassign, pause, reset or abandon one pickwave
    (Picking/UpdatePickingWaveHeader).

    - user_id: assign the wave to this picker (see get_pick_wave_users).
    - unassign=True: remove the picker (sends UserId -1). Not with user_id.
    - state: "Paused", "Unallocated" or "Abandoned" only.
      generate_pick_waves with a user_id creates the wave Allocated;
      update_pick_wave doesn't change state when you reassign. InProgress,
      Complete, Packing and Shipped describe physical work on the warehouse
      floor and are deliberately not settable here.

    Abandoning a STARTED wave, or setting it back to Unallocated, needs
    allow_in_progress=True — a picker may have items on a trolley. Started
    means the wave is InProgress, Paused, Complete or Packing, or has any item
    picked. Pausing doesn't need the flag.

    ⚠️  Abandoning a wave that still holds orders does NOT release them —
    Linnworks keeps them in the abandoned wave, so they can't go into a new
    wave until removed with remove_orders_from_pick_waves (live-confirmed 22
    Sep 2026). Both the dry-run plan and the live result carry a `warning`
    saying exactly that whenever the wave still has orders; an empty wave
    carries none.

    A wave whose current state isn't a documented pickwave state (including a
    missing state) is refused before any write, because the current state is
    sent back and an unknown value could reset it.

    The wave's current state is always sent, even when only the picker
    changes, and its start/end times are carried through: leaving them out
    could reset them on the server. Omitting UserId keeps the current picker
    — verified live 22 Sep 2026. Linnworks applies State literally: it does
    NOT couple state to assignment. Reassigning or unassigning a wave never
    changes its state, and setting state never changes who is assigned — a
    wave can be Allocated with no picker, or Unallocated with a picker still
    on it (both live-confirmed the same day).

    Reads the wave back afterwards (from the Abandoned header list for an
    abandon, because an abandoned wave drops out of the detail endpoint once
    it is empty) and reports outcome updated / not_applied / unconfirmed /
    rate_limited / error. The outcome is judged only on what the call asked
    to change: a user change on the user, a state change on the state, both
    when both were asked. If the server changed the other field anyway, that
    is reported as state_changed_by_server / user_changed_by_server rather
    than as a failure. after_state and after_user_id are always the raw
    read-back values; "no user" reads back as absent, 0 or -1. dry_run=True
    by default.
    """
    if user_id is not None and unassign:
        return {"success": False, "error": "Pass user_id OR unassign=True, not both."}
    if user_id is None and not unassign and state is None:
        return {"success": False, "error": "Nothing to change — pass user_id, unassign=True, or state."}
    if state is not None and state not in _PICK_WAVE_SETTABLE_STATES:
        return {"success": False, "error": (
            f"state must be one of {list(_PICK_WAVE_SETTABLE_STATES)}. generate_pick_waves with a "
            "user_id creates the wave Allocated; update_pick_wave never changes the state when you "
            "reassign. InProgress, Complete, Packing and Shipped describe physical work on the "
            "warehouse floor and are deliberately not settable here.")}
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0):
        return {"success": False, "error": "user_id must be a positive integer (see get_pick_wave_users)."}

    try:
        current = _format_pick_wave_detail(_fetch_pick_wave(picking_wave_id), picking_wave_id)
    except RateLimitError as exc:
        return {"success": False, "picking_wave_id": picking_wave_id,
                "outcome": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"success": False, "picking_wave_id": picking_wave_id,
                "error": f"Reading the wave failed — nothing was written: {exc}"}
    if current is None:
        return {"success": False, "picking_wave_id": picking_wave_id,
                "error": "No live wave with that id. " + _PICK_WAVE_EMPTY_DETAIL_NOTE}

    current_state = current["state"]
    if current_state not in _PICK_WAVE_ALL_STATES:
        return {"success": False, "picking_wave_id": picking_wave_id, "current_state": current_state,
                "error": (f"Wave {picking_wave_id}'s current state {current_state!r} is not a "
                          f"documented pickwave state ({list(_PICK_WAVE_ALL_STATES)}). Nothing was "
                          "written: this tool sends the current state back, and a missing or "
                          "unknown value could reset the wave to the enum default.")}
    items_picked = _as_int(current.get("items_picked")) or 0
    started = current_state in _PICK_WAVE_STARTED_STATES or items_picked > 0
    if state in ("Abandoned", "Unallocated") and started and not allow_in_progress:
        return {"success": False, "picking_wave_id": picking_wave_id, "current_state": current_state,
                "items_picked": current.get("items_picked"),
                "error": (f"Wave {picking_wave_id} has started (state {current_state}, "
                          f"{items_picked} item(s) picked) — a picker may have items on a trolley. "
                          f"Setting it to {state} needs allow_in_progress=True. Pausing is allowed "
                          "without it.")}

    if user_id is not None:
        try:
            roster = _fetch_picker_roster()
        except RateLimitError as exc:
            return _pick_wave_preflight_throttled([{"step": "picker roster", "reason": str(exc)}])
        except RuntimeError as exc:
            return {"success": False,
                    "error": f"Picker roster lookup failed — nothing was written: {exc}"}
        if user_id not in roster:
            return _pick_wave_unknown_user(user_id, roster)

    target_state = state or current_state
    expected_user = None if unassign else (user_id if user_id is not None else current["user_id"])
    location = current.get("location_id") or DEFAULT_LOCATION_ID

    body: dict = {"PickingWaveId": picking_wave_id, "State": target_state}
    if unassign:
        body["UserId"] = -1
    elif user_id is not None:
        body["UserId"] = user_id
    for field, key in (("start_time", "StartTime"), ("end_time", "EndTime")):
        if current.get(field):
            body[key] = current[field]

    plan = {
        "picking_wave_id": picking_wave_id,
        "current_state": current_state,
        "current_user_id": current["user_id"],
        "current_email": current["email_address"],
        "new_state": target_state,
        "new_user_id": expected_user,
        "order_count": len(current["orders"]),
    }
    abandon_warning = (
        {"warning": _PICK_WAVE_ABANDON_WARNING}
        if state == "Abandoned" and plan["order_count"] > 0 else {}
    )
    if dry_run:
        return {"dry_run": True, "plan": plan, **abandon_warning,
                "message": "Dry run — nothing was changed."}

    try:
        _picking_write("Picking/UpdatePickingWaveHeader", body)
    except RateLimitError as exc:
        return {"dry_run": False, "plan": plan, **abandon_warning, "outcome": "rate_limited",
                "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"dry_run": False, "plan": plan, **abandon_warning, "outcome": "error",
                "complete": False, "error": str(exc)}
    except Exception as exc:
        return {"dry_run": False, "plan": plan, **abandon_warning, "outcome": "unconfirmed",
                "complete": False, "error": f"{type(exc).__name__}: {exc}",
                "note": ("The request may have reached Linnworks and the change may have been "
                         "applied. Check get_pick_wave_detail before trying again.")}

    return {"dry_run": False, "plan": plan, **abandon_warning,
            **_read_back_updated_wave(
                picking_wave_id, target_state, expected_user, current["user_id"],
                judge_state=state is not None,
                judge_user=user_id is not None or unassign,
                location_id=location,
            )}


def _locate_orders_in_open_waves(num_ids: list[int], location_id: str) -> dict[int, dict]:
    wanted = set(num_ids)
    found: dict[int, dict] = {}
    for state in _PICK_WAVE_OPEN_STATES:
        resp = call_linnworks_get(
            "Picking/GetAllPickingWaveHeaders", {"state": state, "locationId": location_id}
        )
        for header in (resp.get("PickwaveHeaders") or []) if isinstance(resp, dict) else []:
            wave_id = header.get("PickingWaveId")
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id), wave_id)
            if detail is None:
                continue
            for o in detail["orders"]:
                if o["order_id"] in wanted and o["order_id"] not in found:
                    found[o["order_id"]] = {"picking_wave_id": wave_id, "wave_state": detail["state"]}
    return found


@_tool()
def remove_orders_from_pick_waves(
    order_ids: list,
    location_id: str = DEFAULT_LOCATION_ID,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take orders out of whatever pickwave holds them
    (Picking/DeleteOrdersFromPickingWaves). The orders themselves are not
    changed — they just stop being in a wave, and can go into a new one.

    The manifest shows which wave holds each order, and warns when that wave
    has started — InProgress, Paused, Complete or Packing (a picker may
    already have the items on a trolley). Orders can be GUIDs or order
    numbers; an unresolvable id is reported, not raised. Staged above 25
    orders. dry_run=True by default.

    ⚠️  Linnworks' delete is neither location- nor state-scoped. An order the
    manifest didn't find in an open wave at this location may still be held
    by an ABANDONED wave (abandoning doesn't release orders — see
    update_pick_wave) or a wave at another location, and this call frees it
    either way — live-confirmed for an abandoned wave, 22 Sep 2026.

    Needs the DeletePickingWavesNode permission, which no read tool exercises
    — a missing permission comes back as outcome "error" with Linnworks'
    message verbatim.

    Each affected wave is re-read afterwards. Per-order outcome: removed,
    not_in_a_wave (Linnworks' NoPickwaves list), still_present (the read-back
    still finds it), or unconfirmed (the read-back failed — the row carries
    readback_error — or Linnworks' reply didn't say).

    There is no endpoint to delete a wave itself. Removing a wave's last
    order makes Linnworks abandon the wave automatically (live-confirmed 22
    Sep 2026) — there is nothing more to do to retire it.
    """
    if not order_ids:
        return {"success": False, "error": "order_ids is empty."}

    resolved, resolve_errors, rate_limited = _resolve_order_numbers(order_ids)
    if rate_limited:
        return _pick_wave_preflight_throttled(rate_limited)
    nums = [r[1] for r in resolved]
    try:
        holding = _locate_orders_in_open_waves(nums, location_id) if nums else {}
    except RateLimitError as exc:
        return _pick_wave_preflight_throttled([{"step": "locating waves", "reason": str(exc)}])
    except RuntimeError as exc:
        return {"success": False, "error": f"Locating the orders' waves failed — nothing was written: {exc}"}

    manifest: list[dict] = []
    for input_id, num, guid in resolved:
        where = holding.get(num)
        row = {
            "order_id": input_id,
            "num_order_id": num,
            "order_guid": guid,
            "picking_wave_id": where["picking_wave_id"] if where else None,
            "wave_state": where["wave_state"] if where else None,
        }
        if where and where["wave_state"] in _PICK_WAVE_STARTED_STATES:
            row["warning"] = (f"Wave {where['picking_wave_id']} has started ({where['wave_state']}) "
                              "— a picker may already have this order's items on a trolley.")
        if not where:
            row["note"] = (
                "Not found in any open wave at this location. It may still be held by an "
                "ABANDONED wave (abandoning doesn't release orders) or a wave at another "
                "location — Linnworks' delete is neither state- nor location-scoped and frees "
                "it either way (live-confirmed for an abandoned wave, 22 Sep 2026)."
            )
        manifest.append(row)

    guard = _write_guard("remove_orders_from_pick_waves", resolved, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "resolve_errors": resolve_errors}
    if dry_run:
        in_waves = sum(1 for m in manifest if m["picking_wave_id"])
        return {
            "dry_run": True,
            "order_count": len(order_ids),
            "manifest": manifest,
            "resolve_errors": resolve_errors,
            "message": f"Dry run — {in_waves} order(s) found in open waves. Nothing was removed.",
        }
    if not nums:
        return {"dry_run": False, "results": [], "resolve_errors": resolve_errors,
                "complete": False, "message": "No order resolved; nothing was sent."}

    try:
        resp = _picking_write("Picking/DeleteOrdersFromPickingWaves", {"OrderIds": nums})
    except RateLimitError as exc:
        return {"dry_run": False, "manifest": manifest, "outcome": "rate_limited",
                "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"dry_run": False, "manifest": manifest, "outcome": "error",
                "complete": False, "error": str(exc)}
    except Exception as exc:
        return {"dry_run": False, "manifest": manifest, "outcome": "unconfirmed",
                "complete": False, "error": f"{type(exc).__name__}: {exc}",
                "note": ("The request may have reached Linnworks and the removal may have "
                         "happened. Check get_pick_wave_detail / check_orders_pickable before "
                         "trying again.")}

    if not isinstance(resp, dict):
        resp = {}
    processed = {_as_int(x) for x in (resp.get("ProcessedOrderIds") or [])}
    no_wave = {_as_int(x) for x in (resp.get("NoPickwaves") or [])}

    still_in: set[int] = set()
    readback_errors: dict[int, str] = {}
    for wave_id in sorted({m["picking_wave_id"] for m in manifest if m["picking_wave_id"]}):
        try:
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id), wave_id)
        except RateLimitError as exc:
            readback_errors[wave_id] = f"rate limited: {exc}"
            continue
        except Exception as exc:
            readback_errors[wave_id] = str(exc)
            continue
        if detail is not None:
            still_in.update(o["order_id"] for o in detail["orders"])

    results: list[dict] = []
    for m in manifest:
        num = m["num_order_id"]
        extra: dict = {}
        if num in still_in:
            outcome = "still_present"
        elif m["picking_wave_id"] in readback_errors:
            outcome = "unconfirmed"
            extra["readback_error"] = readback_errors[m["picking_wave_id"]]
        elif num in processed:
            outcome = "removed"
        elif num in no_wave:
            outcome = "not_in_a_wave"
        else:
            outcome = "unconfirmed"
        results.append({**m, "outcome": outcome, **extra})

    complete = not resolve_errors and all(r["outcome"] in ("removed", "not_in_a_wave") for r in results)
    return {
        "dry_run": False,
        "results": results,
        "resolve_errors": resolve_errors,
        "complete": complete,
        "message": f"{sum(r['outcome'] == 'removed' for r in results)} order(s) removed from waves.",
    }


@_tool()
def get_category_report(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
) -> dict:
    """
    Aggregate sales by product category for a date range. Auto-paginates internally
    and returns ranked category totals — revenue, units, and order count. Use this
    instead of get_processed_order_items when you need category-level analysis.
    Much faster than manual pagination.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-04-30".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top categories to return, ranked by revenue. Defaults to 20.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total number of processed orders in the range
          - categories: list of top_n category dicts sorted by revenue desc, each with
              rank, category, revenue, units, orders (distinct order count)
    """
    from collections import defaultdict

    PAGE_SIZE = 500

    cat_revenue: dict[str, float] = defaultdict(float)
    cat_units: dict[str, int] = defaultdict(int)
    cat_order_ids: dict[str, set] = defaultdict(set)

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                cat = item.get("category") or "Uncategorised"
                cat_revenue[cat] += item.get("line_total_inc_tax") or 0.0
                cat_units[cat] += item.get("quantity") or 0
                cat_order_ids[cat].add(guid)

        page += 1

    all_cats = sorted(cat_revenue.keys(), key=lambda c: cat_revenue[c], reverse=True)
    categories = [
        {
            "rank": idx + 1,
            "category": cat,
            "revenue": round(cat_revenue[cat], 2),
            "units": cat_units[cat],
            "orders": len(cat_order_ids[cat]),
        }
        for idx, cat in enumerate(all_cats[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "categories": categories,
    }


def _fetch_revenue_data(
    from_date: str,
    to_date: str,
    date_field: str,
) -> dict:
    PAGE_SIZE = 500

    total_orders = 0
    total_revenue = 0.0
    by_source: dict[str, dict] = {}
    by_country: dict[str, dict] = {}

    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        for o in raw_orders:
            charge = float(o.get("fTotalCharge") or 0)
            total_revenue += charge

            source = o.get("Source") or "Unknown"
            if source not in by_source:
                by_source[source] = {"orders": 0, "revenue": 0.0}
            by_source[source]["orders"] += 1
            by_source[source]["revenue"] += charge

            country = o.get("cCountry") or "Unknown"
            if country not in by_country:
                by_country[country] = {"orders": 0, "revenue": 0.0}
            by_country[country]["orders"] += 1
            by_country[country]["revenue"] += charge

        page += 1

    avg_order_value = total_revenue / total_orders if total_orders > 0 else 0.0

    return {
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "avg_order_value": round(avg_order_value, 2),
        "by_source": sorted(
            [
                {
                    "source": src,
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                    "aov": round(v["revenue"] / v["orders"], 2) if v["orders"] > 0 else 0.0,
                }
                for src, v in by_source.items()
            ],
            key=lambda x: -x["revenue"],
        ),
        "by_country": sorted(
            [
                {
                    "country": c,
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                }
                for c, v in by_country.items()
            ],
            key=lambda x: -x["orders"],
        )[:15],
    }


@_tool()
def get_revenue_summary(
    from_date: str,
    to_date: str,
    date_field: str = "received",
) -> dict:
    """
    Total orders, revenue, and average order value for a date range.

    Auto-paginates through all pages internally and returns aggregated totals —
    never overflows context, unlike get_processed_orders. Also breaks down
    orders and revenue by sales channel (source) and country.

    Use this instead of get_processed_orders when the question is about totals:
    "what was our total revenue in April?", "how many orders did we take last
    month?", "what's our average order value?", "how is Shopify vs Amazon?".

    For per-order or per-SKU detail, use get_processed_orders or
    get_processed_order_items instead.

    Note on Amazon FBA revenue: the total_charge field on FBA orders may capture
    the marketplace fee rather than the full sale price, resulting in artificially
    low AOV for that channel. Treat FBA revenue figures as indicative only.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-04-30".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders:     total orders in the period
          - total_revenue:    sum of order charges (£)
          - avg_order_value:  total_revenue / total_orders
          - by_source:        list of {source, orders, revenue, aov} sorted by revenue
          - by_country:       list of {country, orders, revenue} sorted by order count
    """
    data = _fetch_revenue_data(from_date, to_date, date_field)
    return {"from_date": from_date, "to_date": to_date, "date_field": date_field, **data}


@_tool()
def get_top_skus(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
    rank_by: str = "revenue",
    supplier_name: str = "",
) -> dict:
    """
    Aggregate sales by individual SKU for a date range. Auto-paginates
    internally and returns ranked SKU totals — revenue, units, and order count.

    Use this when you need SKU-level analysis: "what are our top-selling
    products?", "which SKUs drove the most revenue last month?", "how many
    units of each product did we sell?".

    Optionally filter to a single supplier's SKUs by passing supplier_name
    (case-insensitive partial match against the primary supplier name on each
    item). Use get_suppliers() to see the full list of supplier names.

    For category-level analysis use get_category_report instead (faster).

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-31".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top SKUs to return. Defaults to 20.
        rank_by: Sort order — "revenue" (default) or "units".
        supplier_name: Optional. Filter results to SKUs whose primary supplier
            name contains this string (case-insensitive). Leave blank for all.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - ranked_by:            the rank_by value used
          - supplier_filter:      the supplier_name filter applied (or "")
          - skus: list of top_n SKU dicts sorted by rank_by desc, each with
              rank, sku, title, supplier, revenue, units, orders
    """
    from collections import defaultdict

    if rank_by not in ("revenue", "units"):
        rank_by = "revenue"

    PAGE_SIZE = 500
    supplier_filter = supplier_name.strip().lower()

    sku_revenue: dict[str, float] = defaultdict(float)
    sku_units: dict[str, int] = defaultdict(int)
    sku_order_ids: dict[str, set] = defaultdict(set)
    sku_titles: dict[str, str] = {}
    sku_stock_ids: dict[str, str] = {}

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                sku = item.get("sku") or "UNKNOWN"
                qty = item.get("quantity") or 0
                rev = float(item.get("line_total_inc_tax") or 0)
                sku_revenue[sku] += rev
                sku_units[sku] += qty
                sku_order_ids[sku].add(guid)
                if sku not in sku_titles and item.get("title"):
                    sku_titles[sku] = item["title"]
                if sku not in sku_stock_ids and item.get("stock_item_id"):
                    sku_stock_ids[sku] = item["stock_item_id"]

        page += 1

    unique_ids = list({v for v in sku_stock_ids.values() if v})
    supplier_map: dict[str, dict] = {}
    if unique_ids:
        supplier_map = _fetch_supplier_for_items(unique_ids)
    sku_supplier: dict[str, str] = {}
    for sku, sid in sku_stock_ids.items():
        sup = supplier_map.get(sid, {})
        sku_supplier[sku] = sup.get("supplier_name") or "No Supplier"

    sort_key = (lambda s: sku_revenue[s]) if rank_by == "revenue" else (lambda s: sku_units[s])
    all_skus = sorted(sku_revenue.keys(), key=sort_key, reverse=True)

    if supplier_filter:
        all_skus = [s for s in all_skus if supplier_filter in sku_supplier.get(s, "").lower()]

    skus = [
        {
            "rank": idx + 1,
            "sku": sku,
            "title": sku_titles.get(sku, ""),
            "supplier": sku_supplier.get(sku, ""),
            "revenue": round(sku_revenue[sku], 2),
            "units": sku_units[sku],
            "orders": len(sku_order_ids[sku]),
        }
        for idx, sku in enumerate(all_skus[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "ranked_by": rank_by,
        "supplier_filter": supplier_name.strip(),
        "skus": skus,
    }


@_tool()
def get_component_sales(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 50,
    sku: str = "",
) -> dict:
    """
    Aggregate UNIT sales at the composite-child (component) level for a date
    range. Auto-paginates internally.

    Every other sales tool attributes a composite sale entirely to the parent
    SKU, so the real demand for the components hidden inside it (the parts of a kit,
    items in a bundle/multipack, option/linking SKUs) is invisible.
    This tool explodes each composite order line into its components and counts
    the component units, so you can answer "how many of this part did we actually
    sell once bundles and kits are broken out?".

    Units only. Composite children carry no price (the money lives on the parent
    line), so revenue cannot be attributed to a component without a modelling
    assumption — that is deliberately out of scope here.

    How exploding works:
      - A line with composite components contributes its CHILDREN's units, not
        its own. Child quantities are already resolved to the line total
        (e.g. 5 packs x 10 = 50), so they are summed directly.
      - A normal (non-composite) line contributes its own units, so the report
        is complete rather than composites-only.
      - Only leaf items are counted (nested composites recurse), so nothing is
        double-counted.
    Each row reports composite_units vs standalone_units separately so you can
    tell exploded demand from direct sales of the same SKU.

    This is an auto-paginating tool: it can make hundreds of API calls. NEVER
    run it in parallel with another auto-paginating tool (get_top_skus,
    get_sales_by_supplier, get_category_report, get_revenue_summary,
    get_period_comparison) — run them sequentially.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-06-30".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top components to return. Defaults to 50.
        sku: Optional. Restrict output to the components (children) of this one
            composite parent SKU (case-insensitive exact match). Leave blank to
            report every component and standalone SKU across all orders.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - parent_sku_filter:    the sku filter applied (or "")
          - components: list of top_n component dicts sorted by units desc, each
              with rank, sku, title, units, composite_units, standalone_units,
              orders, from_composite (True when all units came from composites)
    """
    from collections import defaultdict

    PAGE_SIZE = 500
    sku_filter = sku.strip().lower()

    comp_units: dict[str, int] = defaultdict(int)
    standalone_units: dict[str, int] = defaultdict(int)
    order_ids: dict[str, set] = defaultdict(set)
    titles: dict[str, str] = {}

    def _credit_leaves(item: dict, guid: str, from_composite: bool) -> None:
        subs = item.get("composite_sub_items") or []
        if subs:
            for s in subs:
                _credit_leaves(s, guid, True)
            return
        leaf_sku = item.get("sku") or "UNKNOWN"
        qty = item.get("quantity") or 0
        if from_composite:
            comp_units[leaf_sku] += qty
        else:
            standalone_units[leaf_sku] += qty
        order_ids[leaf_sku].add(guid)
        if leaf_sku not in titles and item.get("title"):
            titles[leaf_sku] = item["title"]

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                subs = item.get("composite_sub_items") or []
                if sku_filter:
                    if subs and (item.get("sku") or "").lower() == sku_filter:
                        for s in subs:
                            _credit_leaves(s, guid, True)
                else:
                    _credit_leaves(item, guid, False)

        page += 1

    all_skus = set(comp_units) | set(standalone_units)

    def total_units(s: str) -> int:
        return comp_units.get(s, 0) + standalone_units.get(s, 0)

    ranked = sorted(all_skus, key=total_units, reverse=True)

    components = []
    for idx, s in enumerate(ranked[:top_n]):
        cu = comp_units.get(s, 0)
        su = standalone_units.get(s, 0)
        components.append({
            "rank": idx + 1,
            "sku": s,
            "title": titles.get(s, ""),
            "units": cu + su,
            "composite_units": cu,
            "standalone_units": su,
            "orders": len(order_ids[s]),
            "from_composite": cu > 0 and su == 0,
        })

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "parent_sku_filter": sku.strip(),
        "components": components,
    }


@_tool()
def get_sales_by_supplier(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
    rank_by: str = "revenue",
) -> dict:
    """
    Aggregate sales by supplier for a date range. Auto-paginates internally
    and returns ranked supplier totals — revenue, units, order count, and
    the number of distinct SKUs sold.

    Use this when you need supplier-level sales analysis: "which suppliers
    drove the most revenue last month?", "how much did we sell from <supplier>?",
    "which supplier's products sell best by volume?".

    Supplier assignment is based on the primary (IsDefault) supplier linked to
    each SKU in Linnworks inventory. SKUs with no supplier are grouped under
    "No Supplier".

    Use get_suppliers() to see all configured supplier names.
    Use get_top_skus(supplier_name=...) to drill into a specific supplier's SKUs.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-31".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top suppliers to return. Defaults to 20.
        rank_by: Sort order — "revenue" (default) or "units".

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - ranked_by: the rank_by value used
          - suppliers: list of top_n supplier dicts sorted by rank_by desc, each with
              rank, supplier_name, supplier_id, revenue, units, orders, sku_count
    """
    from collections import defaultdict

    if rank_by not in ("revenue", "units"):
        rank_by = "revenue"

    PAGE_SIZE = 500

    sku_revenue: dict[str, float] = defaultdict(float)
    sku_units: dict[str, int] = defaultdict(int)
    sku_order_ids: dict[str, set] = defaultdict(set)
    sku_stock_ids: dict[str, str] = {}

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                sku = item.get("sku") or "UNKNOWN"
                qty = item.get("quantity") or 0
                rev = float(item.get("line_total_inc_tax") or 0)
                sku_revenue[sku] += rev
                sku_units[sku] += qty
                sku_order_ids[sku].add(guid)
                if sku not in sku_stock_ids and item.get("stock_item_id"):
                    sku_stock_ids[sku] = item["stock_item_id"]

        page += 1

    unique_ids = list({v for v in sku_stock_ids.values() if v})
    supplier_map: dict[str, dict] = {}
    if unique_ids:
        supplier_map = _fetch_supplier_for_items(unique_ids)

    sup_revenue: dict[str, float] = defaultdict(float)
    sup_units: dict[str, int] = defaultdict(int)
    sup_order_ids: dict[str, set] = defaultdict(set)
    sup_sku_count: dict[str, set] = defaultdict(set)
    sup_ids: dict[str, str] = {}

    for sku in sku_revenue:
        sid = sku_stock_ids.get(sku, "")
        sup_info = supplier_map.get(sid, {})
        sup_name = sup_info.get("supplier_name") or "No Supplier"
        sup_id = sup_info.get("supplier_id") or ""
        sup_revenue[sup_name] += sku_revenue[sku]
        sup_units[sup_name] += sku_units[sku]
        sup_order_ids[sup_name] |= sku_order_ids[sku]
        sup_sku_count[sup_name].add(sku)
        if sup_name not in sup_ids:
            sup_ids[sup_name] = sup_id

    sort_key = (lambda s: sup_revenue[s]) if rank_by == "revenue" else (lambda s: sup_units[s])
    all_sups = sorted(sup_revenue.keys(), key=sort_key, reverse=True)

    suppliers = [
        {
            "rank": idx + 1,
            "supplier_name": sup,
            "supplier_id": sup_ids.get(sup, ""),
            "revenue": round(sup_revenue[sup], 2),
            "units": sup_units[sup],
            "orders": len(sup_order_ids[sup]),
            "sku_count": len(sup_sku_count[sup]),
        }
        for idx, sup in enumerate(all_sups[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "ranked_by": rank_by,
        "suppliers": suppliers,
    }


@_tool()
def get_period_comparison(
    current_from: str,
    current_to: str,
    prior_from: str,
    prior_to: str,
    date_field: str = "received",
) -> dict:
    """
    Compare revenue and order volume between two date ranges side-by-side.

    Returns totals for both periods and the absolute and percentage change for
    orders, revenue, and average order value. Useful for month-on-month,
    week-on-week, or year-on-year comparisons.

    Examples: "how does this month compare to last month?", "are we up or down
    vs the same period last year?", "what's the MoM revenue change?".

    Args:
        current_from: Start of the current/comparison period, e.g. "2026-05-01".
        current_to: End of the current/comparison period, e.g. "2026-05-31".
        prior_from: Start of the prior/baseline period, e.g. "2026-04-01".
        prior_to: End of the prior/baseline period, e.g. "2026-04-30".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".

    Returns:
        A dict with:
          - current / prior: revenue summary dicts for each period
          - changes: {orders_delta, orders_pct, revenue_delta, revenue_pct,
                      aov_delta, aov_pct} — positive = current better than prior
    """
    current = _fetch_revenue_data(current_from, current_to, date_field)
    prior = _fetch_revenue_data(prior_from, prior_to, date_field)

    def _pct(new: float, old: float) -> float | None:
        if old == 0:
            return None
        return round((new - old) / old * 100, 1)

    return {
        "current": {"from_date": current_from, "to_date": current_to, **current},
        "prior": {"from_date": prior_from, "to_date": prior_to, **prior},
        "date_field": date_field,
        "changes": {
            "orders_delta": current["total_orders"] - prior["total_orders"],
            "orders_pct": _pct(current["total_orders"], prior["total_orders"]),
            "revenue_delta": round(current["total_revenue"] - prior["total_revenue"], 2),
            "revenue_pct": _pct(current["total_revenue"], prior["total_revenue"]),
            "aov_delta": round(current["avg_order_value"] - prior["avg_order_value"], 2),
            "aov_pct": _pct(current["avg_order_value"], prior["avg_order_value"]),
        },
    }


@_tool()
def get_suppliers() -> dict:
    """
    List all suppliers configured in Linnworks.

    Returns every supplier with their ID and name. Useful for resolving a
    supplier name to a GUID before filtering purchase orders by supplier,
    or for answering "what suppliers do we have?" and "what is the ID for
    supplier X?".

    Returns:
        A dict with:
          - count:     total number of suppliers
          - suppliers: list of supplier records with id and name
    """
    suppliers = call_linnworks_get("Inventory/GetSuppliers")

    if isinstance(suppliers, list):
        return {
            "count": len(suppliers),
            "suppliers": [
                {
                    "supplier_id": s.get("pkSupplierID") or s.get("SupplierId") or s.get("Id"),
                    "name": s.get("SupplierName") or s.get("Name") or s.get("name"),
                    "code": s.get("SupplierCode") or s.get("Code"),
                    "currency": s.get("Currency"),
                }
                for s in suppliers
            ],
        }

    for key in ("Suppliers", "Data", "Result"):
        if isinstance(suppliers, dict) and key in suppliers:
            items = suppliers[key]
            return {
                "count": len(items),
                "suppliers": [
                    {
                        "supplier_id": s.get("pkSupplierID") or s.get("SupplierId") or s.get("Id"),
                        "name": s.get("SupplierName") or s.get("Name") or s.get("name"),
                        "code": s.get("SupplierCode") or s.get("Code"),
                        "currency": s.get("Currency"),
                    }
                    for s in items
                ],
            }

    return {"error": "Unexpected response shape from suppliers endpoint", "raw": suppliers}


def _po_line_cost_inc_tax(unit_cost_ex_tax: float, quantity: int, tax_rate: float) -> float:
    return round(unit_cost_ex_tax * quantity * (1 + tax_rate / 100.0), 4)


def _po_line_unit_ex_tax(stored_cost_inc_tax: float, quantity: int, tax_rate: float) -> float:
    q = quantity or 1
    denom = (1 + (tax_rate or 0.0) / 100.0) or 1.0
    return round(stored_cost_inc_tax / denom / q, 4)


@_tool()
def create_purchase_order(
    supplier_id: str,
    items: str,
    location_id: str = "00000000-0000-0000-0000-000000000000",
    external_invoice_number: str = "",
    supplier_reference: str = "",
    quoted_delivery_date: str = "",
    currency: str = "GBP",
    dry_run: bool = True,
) -> dict:
    """
    Create a new purchase order in PENDING status and add line items to it.

    Two-step process: first creates the PO header, then adds each line item
    by resolving each SKU to a stock item GUID. Use open_purchase_order()
    afterwards to move the PO from PENDING to OPEN.

    IMPORTANT: dry_run defaults to True. It will resolve all SKUs and show
    exactly what would be created without writing anything. Set dry_run=False
    only after confirming the resolved items look correct.

    Args:
        supplier_id: UUID of the supplier (pkSupplierID). Use get_suppliers()
            to look up the correct UUID by name.
        items: JSON array of line items to add. Each item must have:
            - "sku":       the product SKU (will be resolved to a stock item ID)
            - "quantity":  integer quantity to order
            - "cost":      unit cost excluding tax (e.g. 10.50)
            - "tax_rate":  tax percentage (e.g. 20.0 for 20%). Defaults to 20.0.
            Example: '[{"sku":"ABC-123","quantity":5,"cost":10.50,"tax_rate":20.0}]'
        location_id: UUID of the destination warehouse location. Defaults to
            the "Default" (all stock) location.
        supplier_reference: YOUR reference for this order as quoted to the supplier
            — e.g. "TEST-PO001". This is the field staff and suppliers see. Leave
            blank if not needed.
        external_invoice_number: Linnworks auto-generates its own PO number here.
            Leave blank in almost all cases — only set this if you need to override
            the Linnworks-generated reference.
        quoted_delivery_date: Expected delivery date in ISO format, e.g. "2026-06-01".
        currency: Currency code, e.g. "GBP". Defaults to "GBP".
        dry_run: If True (default), resolves SKUs and shows what would be created
            without writing anything. Set to False to create the PO.

    Note: DateOfPurchase is always set to the current date/time at creation.
    The Linnworks stored procedure requires it — null or missing causes a SQL
    overflow error. This mirrors the UI behaviour where today's date is the default.

    Returns:
        A dict with:
          - dry_run:       whether this was a dry run
          - status:        "dry_run", "created", or "error"
          - purchase_id:   UUID of the new PO (live runs only)
          - resolved_items: the items with resolved stock_item_ids and totals
          - errors:        list of SKUs that could not be resolved (if any)
          - header:        the PO header fields that were/would be submitted
    """
    import json as _json

    try:
        item_list = _json.loads(items)
        if not isinstance(item_list, list) or len(item_list) == 0:
            return {"status": "error", "error": "items must be a non-empty JSON array."}
    except Exception as exc:
        return {"status": "error", "error": f"Could not parse items JSON: {exc}"}

    resolved = []
    errors = []
    for item in item_list:
        sku = item.get("sku", "").strip()
        if not sku:
            errors.append({"sku": sku, "error": "empty SKU"})
            continue
        try:
            inv = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
            stock_item_id = (inv or {}).get("StockItemId")
            if not stock_item_id:
                errors.append({"sku": sku, "error": "SKU not found in Linnworks"})
                continue
        except Exception as exc:
            errors.append({"sku": sku, "error": str(exc)})
            continue

        qty = int(item.get("quantity", 1))
        cost = float(item.get("cost", 0.0))
        tax_rate = float(item.get("tax_rate", 20.0))
        tax = round(cost * qty * (tax_rate / 100), 4)
        resolved.append({
            "sku": sku,
            "stock_item_id": stock_item_id,
            "quantity": qty,
            "cost": cost,
            "tax_rate": tax_rate,
            "line_total_ex_tax": round(cost * qty, 4),
            "tax": tax,
            "line_total_inc_tax": _po_line_cost_inc_tax(cost, qty, tax_rate),
        })

    if errors:
        return {
            "status": "error",
            "error": f"{len(errors)} SKU(s) could not be resolved — fix these before creating the PO.",
            "errors": errors,
            "resolved_items": resolved,
        }

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    header_fields: dict = {
        "fkSupplierId": supplier_id,
        "fkLocationId": location_id,
        "Currency": currency,
        "DateOfPurchase": today_iso,
        "ExternalInvoiceNumber": external_invoice_number,
        "SupplierReferenceNumber": supplier_reference,
        "QuotedDeliveryDate": f"{quoted_delivery_date}T00:00:00" if quoted_delivery_date else today_iso,
        "ConversionRate": 1.0,
        "PostagePaid": 0.0,
        "ShippingTaxRate": 0.0,
        "UnitAmountTaxIncludedType": 0,
    }

    if dry_run:
        return {
            "dry_run": True,
            "status": "dry_run",
            "message": "No PO created. Set dry_run=False to create.",
            "header": header_fields,
            "resolved_items": resolved,
            "errors": errors,
        }

    create_payload = {"createParameters": header_fields}
    new_po_id = call_linnworks("PurchaseOrder/Create_PurchaseOrder_Initial", create_payload)
    if not new_po_id or not isinstance(new_po_id, str):
        return {
            "status": "error",
            "error": f"Create_PurchaseOrder_Initial returned an unexpected response: {new_po_id!r}",
        }

    item_errors = []
    for r in resolved:
        add_payload = {
            "addItemParameter": {
                "pkPurchaseId": new_po_id,
                "fkStockItemId": r["stock_item_id"],
                "Qty": r["quantity"],
                "Cost": r["line_total_inc_tax"],
                "TaxRate": r["tax_rate"],
                "PackQuantity": 1,
                "PackSize": 1,
            }
        }
        try:
            call_linnworks("PurchaseOrder/Add_PurchaseOrderItem", add_payload)
        except Exception as exc:
            item_errors.append({"sku": r["sku"], "error": str(exc)})

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": new_po_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    result = {
        "dry_run": False,
        "status": "created",
        "purchase_id": new_po_id,
        "linnworks_status": confirmed_header.get("Status"),
        "external_invoice_number": confirmed_header.get("ExternalInvoiceNumber", ""),
        "line_count": confirmed_header.get("LineCount", 0),
        "total_cost": confirmed_header.get("TotalCost") or confirmed_header.get("GrandTotal"),
        "tax_paid": confirmed_header.get("taxPaid"),
        "resolved_items": resolved,
        "header": header_fields,
    }
    if item_errors:
        result["item_errors"] = item_errors
        result["warning"] = "PO header was created but some line items failed — check item_errors."
    return result


@_tool()
def update_purchase_order_header(
    purchase_id: str,
    supplier_id: str = "",
    supplier_reference: str = "",
    external_invoice_number: str = "",
    quoted_delivery_date: str = "",
    date_of_purchase: str = "",
    currency: str = "",
    conversion_rate: float = 0.0,
    dry_run: bool = True,
) -> dict:
    """
    Update the header fields of a purchase order.

    Only works on orders that are not yet Delivered. Reads the current header
    first, applies only the fields you provide, and (unless dry_run=True)
    writes the change back to Linnworks. Always returns a before/after diff
    so you can see exactly what would change.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the change is correct — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order to update (pkPurchaseID).
        supplier_id: The UUID of the supplier (pkSupplierID) to assign to this
            PO. Use get_suppliers() to look up the correct UUID by name.
            Leave blank to keep the current supplier.
        supplier_reference: The supplier's own reference/PO number. Leave blank
            to keep the current value.
        external_invoice_number: The invoice number or your internal PO ref.
            Leave blank to keep the current value.
        quoted_delivery_date: Expected delivery date in ISO format,
            e.g. "2026-06-01". Leave blank to keep the current value.
        date_of_purchase: The purchase date in ISO format, e.g. "2026-05-13".
            Leave blank to keep the current value.
        currency: Currency code, e.g. "GBP". Leave blank to keep current.
        conversion_rate: Exchange rate to GBP. Pass 0.0 to keep current value.
        dry_run: If True (default), returns the proposed changes without writing
            anything to Linnworks. Set to False to apply the update.

    Returns:
        A dict with:
          - purchase_id:  the PO updated
          - dry_run:      whether this was a dry run
          - status:       "dry_run", "updated", or "no_changes"
          - before:       the current header values for changed fields
          - after:        the proposed/applied new values
          - error:        present only if something went wrong
    """
    purchase_id = purchase_id.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}

    current_status = header.get("Status", "")
    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "error": "Cannot update a DELIVERED purchase order.",
            "status": current_status,
        }

    before: dict = {}
    after: dict = {}

    def _register(field_key: str, current_val, new_val):
        if new_val and new_val != current_val:
            before[field_key] = current_val
            after[field_key] = new_val

    _register("fkSupplierId", header.get("fkSupplierId", ""), supplier_id)
    _register("SupplierReferenceNumber", header.get("SupplierReferenceNumber", ""), supplier_reference)
    _register("ExternalInvoiceNumber", header.get("ExternalInvoiceNumber", ""), external_invoice_number)
    _register("Currency", header.get("Currency", ""), currency)

    if quoted_delivery_date:
        _register(
            "QuotedDeliveryDate",
            header.get("QuotedDeliveryDate", ""),
            f"{quoted_delivery_date}T00:00:00",
        )
    if date_of_purchase:
        _register(
            "DateOfPurchase",
            header.get("DateOfPurchase", ""),
            f"{date_of_purchase}T00:00:00",
        )
    if conversion_rate and conversion_rate != header.get("ConversionRate", 0.0):
        before["ConversionRate"] = header.get("ConversionRate")
        after["ConversionRate"] = conversion_rate

    if not after:
        return {
            "purchase_id": purchase_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "No fields to update — all supplied values match the current header.",
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply.",
            "before": before,
            "after": after,
        }

    update_param = {
        "pkPurchaseID": purchase_id,
        "SupplierReferenceNumber": after.get("SupplierReferenceNumber", header.get("SupplierReferenceNumber", "")),
        "ExternalInvoiceNumber": after.get("ExternalInvoiceNumber", header.get("ExternalInvoiceNumber", "")),
        "Currency": after.get("Currency", header.get("Currency", "GBP")),
        "QuotedDeliveryDate": after.get("QuotedDeliveryDate", header.get("QuotedDeliveryDate")),
        "DateOfPurchase": after.get("DateOfPurchase", header.get("DateOfPurchase")),
        "ConversionRate": after.get("ConversionRate", header.get("ConversionRate", 1.0)),
        "fkSupplierId": after.get("fkSupplierId", header.get("fkSupplierId")),
        "fkLocationId": header.get("fkLocationId"),
        "ShippingTaxRate": header.get("ShippingTaxRate", 0.0),
        "PostagePaid": header.get("PostagePaid", 0.0),
    }

    call_linnworks("PurchaseOrder/Update_PurchaseOrderHeader", {"updateParameter": update_param})

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "updated",
        "before": before,
        "after": after,
        "confirmed": {
            k: confirmed_header.get(k) for k in after
        },
    }


@_tool()
def open_purchase_order(
    purchase_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Move a purchase order from PENDING to OPEN status.

    Opening a PO signals to Linnworks that the order has been placed and
    stock is on its way — it populates "Due (On Order)" values in stock
    levels so you can see inbound stock. Use this after creating or
    confirming a PO with your supplier.

    Only works on PENDING orders. OPEN, PARTIAL, and DELIVERED orders are
    rejected with a clear error. Use deliver_purchase_order() to mark
    items as received once they arrive.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you
    are sure — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as
            returned by search_purchase_orders() or get_purchase_order().
        dry_run: If True (default), shows what would happen without writing
            anything. Set to False to apply the status change.

    Returns:
        A dict with:
          - purchase_id:     the PO acted on
          - dry_run:         whether this was a dry run
          - status:          "dry_run", "opened", or "error"
          - from_status:     the status before the change
          - to_status:       "OPEN" (or the confirmed status on read-back)
          - external_invoice_number: the PO reference number, for confirmation
          - error:           present only if the PO cannot be opened
    """
    purchase_id = purchase_id.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")
    ext_inv = header.get("ExternalInvoiceNumber", "")

    if current_status != "PENDING":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": (
                f"Cannot open this PO — current status is {current_status!r}. "
                "Only PENDING orders can be moved to OPEN."
            ),
            "from_status": current_status,
            "external_invoice_number": ext_inv,
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to open this PO.",
            "from_status": current_status,
            "to_status": "OPEN",
            "external_invoice_number": ext_inv,
        }

    call_linnworks(
        "PurchaseOrder/Change_PurchaseOrderStatus",
        {"changeStatusParameter": {"pkPurchaseId": purchase_id, "status": "OPEN"}},
    )

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_status = (confirmed.get("PurchaseOrderHeader") or {}).get("Status", "")

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "opened",
        "from_status": current_status,
        "to_status": confirmed_status,
        "external_invoice_number": ext_inv,
    }


@_tool()
def deliver_purchase_order(
    purchase_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Record delivery of all outstanding items on an OPEN purchase order.

    Marks every undelivered line as fully received, which immediately updates
    stock levels in Linnworks. The PO status will move to DELIVERED (or PARTIAL
    if some lines were already delivered). Linnworks sets the delivery timestamp
    to the current time — it cannot be backdated via the API.

    WARNING: This updates live stock levels and cannot be easily undone.
    dry_run=True (default) shows you what would be delivered without writing
    anything. Always confirm the item list before setting dry_run=False.

    The PO must be in OPEN or PARTIAL status — PENDING orders cannot be
    delivered.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        dry_run: If True (default), shows outstanding items without delivering.
            Set to False to record the delivery and update stock.

    Returns:
        A dict with:
          - purchase_id:         the PO acted on
          - dry_run:             whether this was a dry run
          - status:              "dry_run", "delivered", or "error"
          - outstanding_items:   items that were/would be delivered
          - delivered_header:    updated PO header after delivery (live only)
    """
    purchase_id = purchase_id.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    items = [i for i in (current.get("PurchaseOrderItem") or []) if not i.get("IsDeleted")]

    current_status = header.get("Status", "")
    if current_status not in ("OPEN", "PARTIAL"):
        return {
            "purchase_id": purchase_id,
            "error": f"PO must be OPEN or PARTIAL to deliver. Current status: {current_status}",
            "status": "error",
        }

    outstanding = [
        {
            "sku": i.get("SKU"),
            "title": i.get("ItemTitle"),
            "quantity": i.get("Quantity"),
            "delivered": i.get("Delivered"),
            "outstanding": (i.get("Quantity") or 0) - (i.get("Delivered") or 0),
        }
        for i in items
        if ((i.get("Quantity") or 0) - (i.get("Delivered") or 0)) > 0
    ]

    if not outstanding:
        return {
            "purchase_id": purchase_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "All items on this PO are already fully delivered.",
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No delivery recorded. Set dry_run=False to deliver all items and update stock.",
            "outstanding_items": outstanding,
        }

    response = call_linnworks_form(
        "PurchaseOrder/Deliver_PurchaseItemAll",
        {"pkPurchaseId": purchase_id},
    )

    delivered_header = response.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "delivered",
        "outstanding_items": outstanding,
        "delivered_header": {
            "status": delivered_header.get("Status"),
            "date_of_delivery": delivered_header.get("DateOfDelivery"),
            "delivered_lines_count": delivered_header.get("DeliveredLinesCount"),
            "line_count": delivered_header.get("LineCount"),
        },
    }


def _format_po_note(n: dict) -> dict:
    return {
        "note_id": n.get("pkPurchaseOrderNoteId") or n.get("pkPurchaseNoteId"),
        "note": n.get("Note"),
        "date_created": n.get("NoteDateTime") or n.get("DateCreated"),
        "created_by": n.get("UserName") or n.get("CreatedBy"),
    }


def _fetch_po_notes(purchase_id: str) -> list[dict]:
    raw = call_linnworks("PurchaseOrder/Get_PurchaseOrderNote", {"pkPurchaseId": purchase_id})
    return [_format_po_note(n) for n in raw] if isinstance(raw, list) else []


@_tool()
def add_purchase_order_note(
    purchase_id: str,
    note: str,
    dry_run: bool = True,
) -> dict:
    """
    Add a text note to a purchase order.

    Notes are visible in the Linnworks UI on the PO detail page. Use this to
    record information that has no dedicated field — such as a delivery
    tracking number, courier name, or expected arrival date.

    IMPORTANT: dry_run defaults to True. It confirms the PO exists and shows its
    status and existing notes without writing anything. Set dry_run=False to
    add the note. After a live write the PO's notes are read back, and
    `confirmed` says whether the new note is actually there.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        note: The text content of the note to add.
        dry_run: If True (default), preview only. Set to False to add the note.

    Returns:
        A dict with:
          - purchase_id:   the PO the note was (or would be) added to
          - dry_run:       whether this was a dry run
          - po_status:     the PO's status (PENDING / OPEN / PARTIAL / DELIVERED)
          - existing_notes: notes already on the PO (dry run only)
          - note:          the note text as stored (or to be stored)
          - note_id, date_created, created_by: the new note (live run)
          - confirmed:     True when the read-back finds a new note with this
                           text; False when it doesn't; None if the read-back
                           itself failed (the note may still have been added —
                           check before re-running, or it will be added twice)
    """
    purchase_id = purchase_id.strip()
    if not (note or "").strip():
        raise ValueError("note is empty — nothing to add.")
    _check_injection("note", note)

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    status = (current.get("PurchaseOrderHeader") or {}).get("Status")
    notes_before = _fetch_po_notes(purchase_id)

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "po_status": status,
            "existing_notes": notes_before,
            "note": note,
            "message": "No note written. Set dry_run=False to add this note.",
        }

    response = call_linnworks(
        "PurchaseOrder/Add_PurchaseOrderNote",
        {"pkPurchaseId": purchase_id, "Note": note},
    )
    added = _format_po_note(response if isinstance(response, dict) else {})

    before_ids = {n["note_id"] for n in notes_before}
    try:
        notes_after = _fetch_po_notes(purchase_id)
    except RateLimitError:
        notes_after = None
    except RuntimeError:
        notes_after = None
    if notes_after is None:
        confirmed = None
    else:
        confirmed = any(
            n["note"] == note and n["note_id"] not in before_ids for n in notes_after
        )

    result = {
        "purchase_id": purchase_id,
        "dry_run": False,
        "po_status": status,
        "note_id": added["note_id"],
        "note": added["note"] if added["note"] is not None else note,
        "date_created": added["date_created"],
        "created_by": added["created_by"],
        "confirmed": confirmed,
    }
    if confirmed is None:
        result["warning"] = (
            "The note was sent but the read-back failed, so it is unconfirmed. "
            "Check the PO's notes before re-running, or the note will be added twice."
        )
    elif confirmed is False:
        result["warning"] = (
            "Linnworks accepted the note, but a fresh read of the PO's notes does "
            "not show it. Check the PO in the Linnworks UI."
        )
    return result


@_tool()
def add_purchase_order_item(
    purchase_id: str,
    sku: str,
    quantity: int,
    cost: float,
    tax_rate: float = 20.0,
    pack_quantity: int = 1,
    pack_size: int = 1,
    dry_run: bool = True,
) -> dict:
    """
    Add a new line item to an existing purchase order.

    Resolves the SKU to a Linnworks stock item GUID, then adds it as a new
    line on the PO. Works on PENDING, OPEN, or PARTIAL orders — DELIVERED
    orders are blocked.

    Use this when you need to add an item that was missed when the PO was
    originally created, or when a supplier suggests adding extra SKUs to an
    existing order.

    IMPORTANT: dry_run defaults to True. It resolves the SKU and shows
    exactly what would be added without writing anything. Set dry_run=False
    only after confirming the resolved item looks correct.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as
            returned by search_purchase_orders() or get_purchase_order().
        sku: The exact SKU of the item to add. Must match a Linnworks stock
            item exactly — use find_inventory_item() to verify the SKU first.
        quantity: Number of units to order.
        cost: Unit cost excluding tax (e.g. 10.50).
        tax_rate: Tax percentage (e.g. 20.0 for 20%). Defaults to 20.0.
        pack_quantity: Number of packs. Defaults to 1.
        pack_size: Units per pack. Defaults to 1.
        dry_run: If True (default), resolves the SKU and shows what would be
            added without writing anything. Set to False to add the item.

    Returns:
        A dict with:
          - purchase_id:          the PO being modified
          - dry_run:              whether this was a dry run
          - status:               "dry_run", "added", or "error"
          - item:                 resolved item details (sku, title, stock_item_id,
                                  quantity, cost, tax_rate, line_total_ex_tax, tax)
          - updated_line_count:   number of lines after the change (live only)
          - updated_total_cost:   PO grand total after the change (live only)
    """
    purchase_id = purchase_id.strip()
    sku = sku.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    try:
        inv = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        stock_item_id = (inv or {}).get("StockItemId")
        item_title = (inv or {}).get("ItemTitle", "")
        if not stock_item_id:
            return {
                "purchase_id": purchase_id,
                "status": "error",
                "error": f"SKU {sku!r} not found in Linnworks.",
            }
    except Exception as exc:
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": f"Could not resolve SKU {sku!r}: {exc}",
        }

    line_total = round(cost * quantity, 4)
    tax = round(line_total * (tax_rate / 100), 4)

    item_detail = {
        "sku": sku,
        "title": item_title,
        "stock_item_id": stock_item_id,
        "quantity": quantity,
        "cost": cost,
        "tax_rate": tax_rate,
        "line_total_ex_tax": line_total,
        "tax": tax,
        "line_total_inc_tax": _po_line_cost_inc_tax(cost, quantity, tax_rate),
        "pack_quantity": pack_quantity,
        "pack_size": pack_size,
    }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to add this item.",
            "current_po_status": current_status,
            "item": item_detail,
        }

    call_linnworks(
        "PurchaseOrder/Add_PurchaseOrderItem",
        {
            "addItemParameter": {
                "pkPurchaseId": purchase_id,
                "fkStockItemId": stock_item_id,
                "Qty": quantity,
                "Cost": item_detail["line_total_inc_tax"],
                "TaxRate": tax_rate,
                "PackQuantity": pack_quantity,
                "PackSize": pack_size,
            }
        },
    )

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "added",
        "item": item_detail,
        "updated_line_count": confirmed_header.get("LineCount"),
        "updated_total_cost": confirmed_header.get("GrandTotal") or confirmed_header.get("TotalCost"),
    }


@_tool()
def update_purchase_order_item(
    purchase_id: str,
    purchase_item_id: str,
    quantity: Optional[int] = None,
    cost: Optional[float] = None,
    tax_rate: Optional[float] = None,
    dry_run: bool = True,
) -> dict:
    """
    Edit the quantity, cost, or tax rate of a line item on an existing purchase order.

    Reads the current line item from the PO, applies only the fields you
    provide (leaving others unchanged), and (unless dry_run=True) writes the
    change back. Always returns a before/after diff so you can see exactly
    what would change.

    Works on PENDING, OPEN, or PARTIAL orders — DELIVERED orders are blocked.

    To find purchase_item_id: call get_purchase_order() and look in the
    "items" list — each entry has a "purchase_item_id" field.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the change is correct — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        purchase_item_id: The UUID of the specific line item to update
            (pkPurchaseItemId). Use get_purchase_order() to find this — it
            appears as "purchase_item_id" in each item in the "items" list.
        quantity: New quantity to order. Omit (or pass None) to keep current.
        cost: New unit cost excluding tax (e.g. 12.00). Omit to keep current.
        tax_rate: New tax percentage (e.g. 20.0). Omit to keep current.
        dry_run: If True (default), shows the proposed changes without writing
            anything. Set to False to apply the update.

    Returns:
        A dict with:
          - purchase_id:        the PO being modified
          - purchase_item_id:   the line item updated
          - dry_run:            whether this was a dry run
          - status:             "dry_run", "updated", "no_changes", or "error"
          - sku:                the SKU of the item updated, for confirmation
          - before:             current values of the fields that would change
          - after:              proposed/applied new values for those fields
    """
    purchase_id = purchase_id.strip()
    purchase_item_id = purchase_item_id.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    items_raw = [
        i for i in (current.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    current_item = next(
        (i for i in items_raw if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )
    if current_item is None:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "status": "error",
            "error": (
                f"Line item {purchase_item_id!r} not found on PO {purchase_id!r}. "
                "Use get_purchase_order() to list current items and their IDs."
            ),
        }

    sku = current_item.get("SKU", "")

    before: dict = {}
    after: dict = {}

    current_qty = current_item.get("Quantity") or 0
    current_tax_rate = current_item.get("TaxRate") or 0.0
    current_unit_ex = _po_line_unit_ex_tax(
        current_item.get("Cost") or 0.0, current_qty, current_tax_rate
    )

    new_quantity = current_qty
    new_unit_ex = current_unit_ex
    new_tax_rate = current_tax_rate

    if quantity is not None and quantity != current_qty:
        before["quantity"] = current_qty
        after["quantity"] = quantity
        new_quantity = quantity

    if cost is not None and round(cost, 4) != current_unit_ex:
        before["cost"] = current_unit_ex
        after["cost"] = round(cost, 4)
        new_unit_ex = cost

    if tax_rate is not None and tax_rate != current_tax_rate:
        before["tax_rate"] = current_tax_rate
        after["tax_rate"] = tax_rate
        new_tax_rate = tax_rate

    if not before:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "The values you provided match the current item — no update needed.",
            "sku": sku,
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply this update.",
            "sku": sku,
            "before": before,
            "after": after,
        }

    call_linnworks(
        "PurchaseOrder/Update_PurchaseOrderItem",
        {
            "updateItemParameter": {
                "pkPurchaseItemId": purchase_item_id,
                "pkPurchaseId": purchase_id,
                "Quantity": new_quantity,
                "PackQuantity": current_item.get("PackQuantity", 1),
                "PackSize": current_item.get("PackSize", 1),
                "Cost": _po_line_cost_inc_tax(new_unit_ex, new_quantity, new_tax_rate),
                "TaxRate": new_tax_rate,
            }
        },
    )

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_items = [
        i for i in (confirmed.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    confirmed_item = next(
        (i for i in confirmed_items if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )

    confirmed_after: dict = {}
    if confirmed_item:
        if "quantity" in after:
            confirmed_after["quantity"] = confirmed_item.get("Quantity")
        if "cost" in after:
            confirmed_after["cost"] = _po_line_unit_ex_tax(
                confirmed_item.get("Cost") or 0.0,
                confirmed_item.get("Quantity") or 0,
                confirmed_item.get("TaxRate") or 0.0,
            )
        if "tax_rate" in after:
            confirmed_after["tax_rate"] = confirmed_item.get("TaxRate")

    return {
        "purchase_id": purchase_id,
        "purchase_item_id": purchase_item_id,
        "dry_run": False,
        "status": "updated",
        "sku": sku,
        "before": before,
        "after": confirmed_after or after,
    }


@_tool()
def remove_purchase_order_item(
    purchase_id: str,
    purchase_item_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Remove a line item from an existing purchase order.

    Reads the current item from the PO so you can confirm what will be
    removed, then (unless dry_run=True) deletes the line. Works on PENDING,
    OPEN, or PARTIAL orders — DELIVERED orders are blocked.

    To find purchase_item_id: call get_purchase_order() and look in the
    "items" list — each entry has a "purchase_item_id" field.

    WARNING: Removal cannot be undone via the API. dry_run=True (default)
    shows you exactly what would be removed without writing anything.
    Always confirm with the user before setting dry_run=False.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        purchase_item_id: The UUID of the specific line item to remove
            (pkPurchaseItemId). Use get_purchase_order() to find this — it
            appears as "purchase_item_id" in each item in the "items" list.
        dry_run: If True (default), shows what would be removed without
            writing anything. Set to False to delete the line item.

    Returns:
        A dict with:
          - purchase_id:          the PO being modified
          - purchase_item_id:     the line item removed
          - dry_run:              whether this was a dry run
          - status:               "dry_run", "removed", or "error"
          - removed_item:         the item that was/would be removed
                                  (sku, title, quantity, delivered, cost, tax_rate)
          - remaining_line_count: number of lines remaining after removal (live only)
          - warning:              present if read-back suggests removal may have failed
    """
    purchase_id = purchase_id.strip()
    purchase_item_id = purchase_item_id.strip()

    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    items_raw = [
        i for i in (current.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    target_item = next(
        (i for i in items_raw if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )
    if target_item is None:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "status": "error",
            "error": (
                f"Line item {purchase_item_id!r} not found on PO {purchase_id!r}. "
                "Use get_purchase_order() to list current items and their IDs."
            ),
        }

    removed_item = {
        "sku": target_item.get("SKU"),
        "title": target_item.get("ItemTitle"),
        "quantity": target_item.get("Quantity"),
        "delivered": target_item.get("Delivered"),
        "cost": target_item.get("Cost"),
        "tax_rate": target_item.get("TaxRate"),
    }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to remove this item.",
            "current_po_status": current_status,
            "removed_item": removed_item,
        }

    call_linnworks(
        "PurchaseOrder/Delete_PurchaseOrderItem",
        {
            "deleteItemParameter": {
                "pkPurchaseItemId": purchase_item_id,
                "pkPurchaseId": purchase_id,
            }
        },
    )

    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}
    confirmed_items = [
        i for i in (confirmed.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    still_present = any(
        i.get("pkPurchaseItemId") == purchase_item_id for i in confirmed_items
    )

    result = {
        "purchase_id": purchase_id,
        "purchase_item_id": purchase_item_id,
        "dry_run": False,
        "status": "removed",
        "removed_item": removed_item,
        "remaining_line_count": confirmed_header.get("LineCount"),
    }
    if still_present:
        result["warning"] = (
            "Item still appears in read-back — removal may have failed. "
            "Check the PO in the Linnworks UI."
        )
    return result


@_tool()
def delete_purchase_order(
    purchase_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete one or more entire purchase orders (header + all lines).

    ⚠️  IRREVERSIBLE.  Deleting a PO destroys its header, every line item, and
    its notes — there is no API to restore it.  This is the whole-PO counterpart
    to remove_purchase_order_item (which removes a single line).  Use it to clear
    out mistaken, duplicate, or abandoned draft POs.

    The dry-run manifest and the staging gate are the main safety nets.  Always
    run dry_run=True first and read the manifest carefully before setting
    dry_run=False.

    ⚠️  A DELIVERED (or partially delivered) PO records goods actually received
    into stock — deleting it loses that history.  This tool BLOCKS PENDING/OPEN
    deletion only when you have not confirmed; DELIVERED and PARTIAL POs are
    flagged in the manifest with a "delivered" warning so you can decide, but are
    NOT auto-blocked (Linnworks itself allows the delete).  Prefer to only delete
    PENDING/OPEN draft POs.

    For batches larger than 10 POs this tool enters a staging mode: it returns a
    manifest of exactly what would be destroyed and asks you to confirm with
    confirmed_count=<N> before executing.

    Uses PurchaseOrder/Delete_PurchaseOrder (payload sent UNWRAPPED as
    {"pkPurchaseId": "<guid>"} — empty 2xx body on success; a subsequent
    Get_PurchaseOrder then returns HTTP 400 "Purchase Order does not exist",
    which is used as the per-PO read-back confirmation).

    Args:
        purchase_ids: List of purchase order UUIDs (pkPurchaseID), as returned by
            search_purchase_orders.  Each is read back first to capture a summary;
            a PO id that does not resolve is reported as an error row and does NOT
            abort the rest of the batch.
        confirmed_count: For batches > 10, pass len(purchase_ids) here after
            reviewing the manifest to confirm the destruction.
        dry_run: If True (default), returns the manifest of what would be deleted
            without writing.  Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - po_count:    number of PO ids in the batch
          - manifest:    per-PO preview (purchase_id, status, supplier_id,
                         line_count, total_cost, date_of_purchase, delivered flag,
                         resolved/error) — always present
          - results:     per-PO outcome with deleted flag (live run only)
          - deleted:     count of POs confirmed gone (live run only)
          - errors:      count of PO ids that failed to resolve or delete
    """
    if not purchase_ids:
        raise ValueError("purchase_ids must contain at least one PO id.")

    manifest: list[dict] = []

    for pid in purchase_ids:
        pid = (pid or "").strip()
        if not pid:
            manifest.append({
                "purchase_id": pid, "resolved": False, "error": "empty PO id",
            })
            continue
        try:
            current = call_linnworks(
                "PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": pid}
            )
        except RuntimeError as exc:
            manifest.append({
                "purchase_id": pid, "resolved": False,
                "error": f"not found: {exc}",
            })
            continue

        header = current.get("PurchaseOrderHeader") or {}
        status = header.get("Status", "")
        live_lines = [
            i for i in (current.get("PurchaseOrderItem") or [])
            if not i.get("IsDeleted")
        ]
        row = {
            "purchase_id":      header.get("pkPurchaseID") or pid,
            "status":           status,
            "status_label":     _PO_STATUS_LABELS.get(status, status),
            "supplier_id":      header.get("fkSupplierId"),
            "supplier_reference": header.get("SupplierReferenceNumber"),
            "line_count":       len(live_lines),
            "total_cost":       header.get("TotalCost"),
            "currency":         header.get("Currency"),
            "date_of_purchase": header.get("DateOfPurchase"),
            "delivered":        status in ("DELIVERED", "PARTIAL"),
            "resolved":         True,
        }
        if row["delivered"]:
            row["warning"] = (
                f"PO is {row['status_label']} — deleting loses received-stock "
                "history. Confirm this is intentional."
            )
        manifest.append(row)

    resolved_rows = [m for m in manifest if m.get("resolved")]
    resolve_errors = [m for m in manifest if not m.get("resolved")]

    guard = _write_guard("delete_purchase_order", purchase_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        delivered_n = sum(1 for m in resolved_rows if m.get("delivered"))
        return {
            "dry_run":  True,
            "po_count": len(purchase_ids),
            "manifest": manifest,
            "message": (
                f"Dry run — nothing deleted. {len(resolved_rows)} PO(s) would be "
                f"PERMANENTLY deleted ({delivered_n} of them delivered/partial), "
                f"{len(resolve_errors)} could not be resolved. Review the manifest, "
                "then set dry_run=False to execute. This cannot be undone."
            ),
        }

    if not resolved_rows:
        return {
            "dry_run":  False,
            "po_count": len(purchase_ids),
            "deleted":  0,
            "errors":   len(resolve_errors),
            "results":  [],
            "manifest": manifest,
            "message":  "No PO ids resolved; nothing was deleted.",
        }

    results = []
    deleted = 0
    errors = len(resolve_errors)
    for m in resolved_rows:
        pid = m["purchase_id"]
        delete_error: str | None = None
        try:
            call_linnworks(
                "PurchaseOrder/Delete_PurchaseOrder", {"pkPurchaseId": pid}
            )
        except RuntimeError as exc:
            delete_error = str(exc)

        gone = False
        try:
            call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": pid})
            gone = False
        except RuntimeError:
            gone = True

        if gone:
            deleted += 1
        else:
            errors += 1

        row = {
            "purchase_id":  pid,
            "status_label": m.get("status_label"),
            "line_count":   m.get("line_count"),
            "total_cost":   m.get("total_cost"),
            "deleted":      gone,
        }
        if delete_error is not None:
            row["delete_error"] = delete_error
        if not gone:
            row["note"] = "still exists after delete call"
        results.append(row)

    for m in resolve_errors:
        results.append({
            "purchase_id": m["purchase_id"],
            "deleted":     False,
            "error":       m.get("error"),
        })

    return {
        "dry_run":  False,
        "po_count": len(purchase_ids),
        "deleted":  deleted,
        "errors":   errors,
        "results":  results,
        "manifest": manifest,
    }


_PO_STATUS_LABELS: dict[str, str] = {
    "PENDING": "Pending",
    "OPEN": "Open",
    "PARTIAL": "Partial Delivery",
    "DELIVERED": "Delivered",
}


@_tool()
def search_purchase_orders(
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    search_value: str = "",
    search_type: str = "All",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """
    Search and list purchase orders from Linnworks.

    Returns purchase order headers matching the given filters — status, date
    range, reference number, SKU, or supplier code. Useful for questions like
    "what purchase orders are currently open?", "which POs are due for
    delivery?", "show me pending orders from the last 30 days", or "which
    POs contain SKU ABC-123?".

    Args:
        status: Filter by PO status. One of: "PENDING", "OPEN", "PARTIAL",
            "DELIVERED". Leave blank to return all statuses.
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
            Filters on DateOfPurchase. Leave blank for no lower bound.
        to_date: End of the date range in ISO format, e.g. "2026-05-13".
            Leave blank for no upper bound.
        search_value: A keyword to search for. What it matches depends on
            search_type. Leave blank to skip keyword filtering.
        search_type: What the search_value matches against. One of:
            "All" (default), "Reference", "StockItemSKU", "SupplierCode",
            "SupplierReference". Ignored when search_value is blank.
        page: Page number for paginated results. Defaults to 1.
        page_size: Results per page. Defaults to 50.

    Returns:
        A dict with:
          - page, total_pages, total_count: pagination info
          - count: number of POs on this page
          - purchase_orders: list of PO header summaries
    """
    request_body: dict = {
        "EntriesPerPage": page_size,
        "PageNumber": page,
        "SearchType": search_type,
    }

    if status:
        request_body["Status"] = status.upper()
    if from_date:
        request_body["DateFrom"] = f"{from_date}T00:00:00"
    if to_date:
        request_body["DateTo"] = f"{to_date}T23:59:59"
    if search_value:
        request_body["SearchValue"] = search_value

    response = call_linnworks(
        "PurchaseOrder/Search_PurchaseOrders2",
        request_body,
    )

    result = response.get("Result") or []
    total_pages = response.get("TotalPages", 1)
    total_count = response.get("TotalNumberOfRecords", 0)

    pos = [
        {
            "purchase_id": po.get("pkPurchaseID"),
            "supplier_id": po.get("fkSupplierId"),
            "location_id": po.get("fkLocationId"),
            "status": po.get("Status"),
            "status_label": _PO_STATUS_LABELS.get(po.get("Status", ""), po.get("Status")),
            "currency": po.get("Currency"),
            "external_invoice_number": po.get("ExternalInvoiceNumber"),
            "supplier_reference": po.get("SupplierReferenceNumber"),
            "date_of_purchase": po.get("DateOfPurchase"),
            "quoted_delivery_date": po.get("QuotedDeliveryDate"),
            "date_of_delivery": po.get("DateOfDelivery"),
            "line_count": po.get("LineCount"),
            "delivered_lines_count": po.get("DeliveredLinesCount"),
            "total_cost": po.get("TotalCost"),
            "tax_paid": po.get("taxPaid"),
            "locked": po.get("Locked"),
        }
        for po in result
    ]

    return {
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(pos),
        "purchase_orders": pos,
    }


@_tool()
def get_purchase_order(purchase_id: str) -> dict:
    """
    Fetch full detail for a single purchase order by its ID.

    Returns the PO header, all line items (SKUs, quantities, costs, delivered
    quantities), and any delivery records. Use this to answer questions like
    "show me PO [ID] in full", "how many units of each SKU are on this PO?",
    "how much of this PO has been delivered?", or "what's the total cost and
    tax on this order?".

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as returned
            by search_purchase_orders.

    Returns:
        A dict with:
          - purchase_id: the ID queried
          - header: PO header fields (status, dates, costs, supplier, location)
          - item_count: number of line items
          - items: list of line items with SKU, quantity, cost, delivered qty
          - delivered_records: list of delivery events (if any)
          - note_count: number of notes attached to the PO
    """
    purchase_id = purchase_id.strip()

    response = call_linnworks(
        "PurchaseOrder/Get_PurchaseOrder",
        {"pkPurchaseId": purchase_id},
    )

    header_raw = response.get("PurchaseOrderHeader") or {}
    items_raw = response.get("PurchaseOrderItem") or []
    delivered_raw = response.get("DeliveredRecords") or []

    header = {
        "purchase_id": header_raw.get("pkPurchaseID"),
        "supplier_id": header_raw.get("fkSupplierId"),
        "location_id": header_raw.get("fkLocationId"),
        "status": header_raw.get("Status"),
        "status_label": _PO_STATUS_LABELS.get(header_raw.get("Status", ""), header_raw.get("Status")),
        "currency": header_raw.get("Currency"),
        "external_invoice_number": header_raw.get("ExternalInvoiceNumber"),
        "supplier_reference": header_raw.get("SupplierReferenceNumber"),
        "date_of_purchase": header_raw.get("DateOfPurchase"),
        "quoted_delivery_date": header_raw.get("QuotedDeliveryDate"),
        "date_of_delivery": header_raw.get("DateOfDelivery"),
        "line_count": header_raw.get("LineCount"),
        "delivered_lines_count": header_raw.get("DeliveredLinesCount"),
        "total_cost": header_raw.get("TotalCost"),
        "tax_paid": header_raw.get("taxPaid"),
        "conversion_rate": header_raw.get("ConversionRate"),
        "converted_grand_total": header_raw.get("ConvertedGrandTotal"),
        "locked": header_raw.get("Locked"),
    }

    items = [
        {
            "purchase_item_id": i.get("pkPurchaseItemId"),
            "stock_item_id": i.get("fkStockItemId"),
            "sku": i.get("SKU"),
            "title": i.get("ItemTitle"),
            "supplier_code": i.get("SupplierCode"),
            "bin_rack": i.get("BinRack"),
            "quantity": i.get("Quantity"),
            "delivered": i.get("Delivered"),
            "outstanding": (i.get("Quantity") or 0) - (i.get("Delivered") or 0),
            "cost": i.get("Cost"),
            "line_total_inc_tax": i.get("Cost"),
            "unit_cost_ex_tax": _po_line_unit_ex_tax(
                i.get("Cost") or 0.0, i.get("Quantity") or 0, i.get("TaxRate") or 0.0
            ),
            "tax_rate": i.get("TaxRate"),
            "tax": i.get("Tax"),
            "pack_quantity": i.get("PackQuantity"),
            "pack_size": i.get("PackSize"),
        }
        for i in items_raw
        if not i.get("IsDeleted")
    ]

    delivered = [
        {
            "stock_item_id": d.get("fkStockItemId"),
            "sku": d.get("SKU"),
            "quantity_delivered": d.get("Qty"),
            "delivery_date": d.get("DeliveryDate"),
        }
        for d in delivered_raw
    ]

    return {
        "purchase_id": purchase_id,
        "header": header,
        "item_count": len(items),
        "items": items,
        "delivered_records": delivered,
        "note_count": response.get("NoteCount", 0),
    }


@_tool()
def get_rules() -> dict:
    """
    List all rules configured in the Linnworks Rules Engine.

    The Rules Engine applies automatic changes to orders as they arrive —
    assigning shipping services, routing to folders, locking/parking orders,
    tagging, running macros, and more. Each rule has an ordered list of
    conditions, and the first matching condition's action fires.

    Returns every rule with its ID, name, type, enabled state, run order,
    and draft status. Use this to answer questions like "what rules do we
    have set up?", "which rules are currently enabled?", "what order do
    rules run in?", or "are any rules in draft?".

    Rule types: "Orders" (fires on incoming orders), "Test" (sandbox rules).

    Returns:
        A dict with:
          - count:  total number of rules
          - rules:  list of rule summaries, each with pk_rule_id, rule_name,
                    rule_type, enabled, run_order, draft, pk_rule_id_draft
    """
    response = call_linnworks_get("RulesEngine/GetRules")

    rules = response if isinstance(response, list) else []

    return {
        "count": len(rules),
        "rules": [
            {
                "pk_rule_id": r.get("pkRuleId"),
                "rule_name": r.get("RuleName"),
                "rule_type": r.get("RuleType"),
                "rule_type_display": r.get("RuleTypeDisplayName"),
                "enabled": r.get("Enabled"),
                "run_order": r.get("RunOrder"),
                "draft": r.get("Draft"),
                "pk_rule_id_draft": r.get("pkRuleId_Draft"),
            }
            for r in rules
        ],
    }


def _format_condition_tree(node: dict) -> dict:
    conditions = [
        {
            "field": c.get("FieldName"),
            "evaluation": c.get("Evaluation"),
            "key_value": c.get("KeyValue"),
            "values": c.get("Values") or [],
        }
        for c in (node.get("Conditions") or [])
    ]

    raw_action = node.get("Action") or {}
    action = None
    if raw_action.get("pkActionId"):
        action = {
            "pk_action_id": raw_action.get("pkActionId"),
            "action_name": raw_action.get("ActionName"),
            "action_type": raw_action.get("ActionType"),
            "action_value": raw_action.get("ActionValue"),
            "properties": [
                {
                    "name": p.get("DisplayName"),
                    "value": p.get("Value"),
                }
                for p in (raw_action.get("Properties") or [])
            ],
        }

    subrules = [
        _format_condition_tree(sub)
        for sub in (node.get("Subrules") or [])
    ]

    return {
        "pk_condition_id": node.get("pkConditionId"),
        "condition_name": node.get("ConditionName"),
        "run_order": node.get("RunOrder"),
        "enabled": node.get("Enabled"),
        "conditions": conditions,
        "action": action,
        "subrules": subrules,
    }


@_tool()
def get_rule(rule_id: int) -> dict:
    """
    Fetch the full condition and action tree for a single Rules Engine rule.

    Returns the complete "IF [conditions] THEN [action]" logic for every
    branch of the rule, including nested subrules. Use this to answer questions
    like "what does this rule actually do?", "what conditions trigger the
    shipping assignment rule?", "which rule assigns orders to the HLC folder?",
    or "why is this order being routed to a particular service?".

    Each condition node has:
      - conditions: the IF clauses (field + evaluator + values)
      - action:     the THEN clause (action type + properties)
      - subrules:   nested condition branches (else-if chains)

    Common action types: AssignShippingService, AssignToFolder,
    AssignToLocation, AssignTagToOrder, ChangeOrderParkStatus,
    ChangeOrderLockStatus, ExecuteMacro, AddNoteToOrder, SetDispatchDate.

    Args:
        rule_id: The integer ID of the rule (pk_rule_id from get_rules).

    Returns:
        A dict with:
          - pk_rule_id:   the rule queried
          - condition_count: number of top-level condition nodes
          - conditions:  list of condition trees, each with conditions,
                         action, and nested subrules
    """
    response = call_linnworks_get(
        "RulesEngine/GetRuleConditionNodes",
        params={"pkRuleId": rule_id},
    )

    nodes = response if isinstance(response, list) else []

    return {
        "pk_rule_id": rule_id,
        "condition_count": len(nodes),
        "conditions": [_format_condition_tree(n) for n in nodes],
    }


@_tool()
def get_import_list() -> dict:
    """
    List all configured import tasks in Linnworks.

    Returns every scheduled import with its ID, friendly name, type, enabled
    state, current execution status, last run timestamps, last import status,
    and next scheduled run time. Use this to answer questions like "what imports
    do we have set up?", "which imports are currently enabled?", "when did the
    stock level import last run?", or "is any import currently executing or
    queued?". Also useful for debugging — check ImportStatus and Completed to
    spot failed or stale imports.

    Returns:
        A dict with:
          - count:   total number of configured imports
          - imports: list of import summaries, each with id, friendly_name, type,
                     enabled, executing, is_queued, started, completed,
                     import_status, import_skipped, all_schedules_disabled,
                     next_schedule, schedule_count
    """
    response = call_linnworks_get("ImportExport/GetImportList")

    register = response.get("register") if isinstance(response, dict) else response
    if not isinstance(register, list):
        register = []

    imports = [
        {
            "id": r.get("Id"),
            "friendly_name": r.get("FriendlyName"),
            "type": r.get("Type"),
            "enabled": r.get("Enabled"),
            "executing": r.get("Executing"),
            "is_queued": r.get("IsQueued"),
            "started": r.get("Started"),
            "completed": r.get("Completed"),
            "import_status": r.get("ImportStatus"),
            "import_skipped": r.get("ImportSkipped"),
            "all_schedules_disabled": r.get("AllSchedulesDisabled"),
            "next_schedule": r.get("NextSchedule"),
            "schedule_count": len(r.get("Schedules") or []),
        }
        for r in register
    ]

    return {"count": len(imports), "imports": imports}


@_tool()
def get_export_list() -> dict:
    """
    List all configured export tasks in Linnworks.

    Returns every scheduled export with its ID, friendly name, type, enabled
    state, current execution status, last run timestamps, last export status,
    and next scheduled run time. Use this to answer questions like "what exports
    do we have set up?", "which exports are enabled?", "when did the inventory
    export last run?", or "is any export currently failing?". Also useful for
    debugging — check last_export_status and completed to spot issues.

    Returns:
        A dict with:
          - count:   total number of configured exports
          - exports: list of export summaries, each with id, friendly_name, type,
                     enabled, executing, is_queued, started, completed,
                     last_export_status, all_schedules_disabled, next_schedule,
                     schedule_count
    """
    response = call_linnworks_get("ImportExport/GetExportList")

    register = response.get("register") if isinstance(response, dict) else response
    if not isinstance(register, list):
        register = []

    exports = [
        {
            "id": r.get("Id"),
            "friendly_name": r.get("FriendlyName"),
            "type": r.get("Type"),
            "enabled": r.get("Enabled"),
            "executing": r.get("Executing"),
            "is_queued": r.get("IsQueued"),
            "started": r.get("Started"),
            "completed": r.get("Completed"),
            "last_export_status": r.get("LastExportStatus"),
            "all_schedules_disabled": r.get("AllSchedulesDisabled"),
            "next_schedule": r.get("NextSchedule"),
            "schedule_count": len(r.get("Schedules") or []),
        }
        for r in register
    ]

    return {"count": len(exports), "exports": exports}


@_tool()
def get_import(import_id: int) -> dict:
    """
    Fetch full detail for a single configured import task.

    Returns the complete import configuration including the specification
    (file source, column mappings, field settings), the register (current
    status and timestamps), and all schedule entries with their cron
    configuration. Use this when you need to inspect exactly how an import
    is configured — e.g. "what file path is the stock level import reading
    from?", "what columns are mapped on this import?", or "what schedule is
    this import running on?".

    Args:
        import_id: The integer ID of the import (as returned by get_import_list).

    Returns:
        A dict with:
          - id:            the import ID queried
          - friendly_name: human-readable import name
          - type:          the import type (e.g. "StockLevel", "Inventory")
          - enabled:       whether the import is enabled
          - executing:     whether it is currently running
          - is_queued:     whether it is queued for execution
          - started:       last start timestamp
          - completed:     last completion timestamp
          - import_status: last import status string
          - import_skipped: whether the last run was skipped (no file change)
          - next_schedule: next scheduled run time
          - schedules:     list of schedule entries with their configuration
          - specification: full specification dict (file source, column mappings, etc.)
    """
    response = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})

    register = response.get("Register") or {}
    schedules = response.get("Schedules") or []
    spec = response.get("Specification") or {}

    return {
        "id": register.get("Id"),
        "friendly_name": register.get("FriendlyName"),
        "type": register.get("Type"),
        "enabled": register.get("Enabled"),
        "executing": register.get("Executing"),
        "is_queued": register.get("IsQueued"),
        "started": register.get("Started"),
        "completed": register.get("Completed"),
        "import_status": register.get("ImportStatus"),
        "import_skipped": register.get("ImportSkipped"),
        "next_schedule": register.get("NextSchedule"),
        "all_schedules_disabled": register.get("AllSchedulesDisabled"),
        "schedules": [
            {
                "schedule_id": s.get("Id"),
                "name": s.get("Name"),
                "schedule_xml": s.get("ScheduleXML"),
                "configuration": s.get("Configuration"),
            }
            for s in schedules
        ],
        "specification": spec,
    }


@_tool()
def get_export(export_id: int) -> dict:
    """
    Fetch full detail for a single configured export task.

    Returns the complete export configuration including the specification
    (destination, query/filter settings), the register (current status and
    timestamps), and all schedule entries with their cron configuration.
    Use this when you need to inspect exactly how an export is configured —
    e.g. "where is this export sending data?", "what filters does this export
    apply?", or "what schedule is this export running on?".

    Args:
        export_id: The integer ID of the export (as returned by get_export_list).

    Returns:
        A dict with:
          - id:               the export ID queried
          - friendly_name:    human-readable export name
          - type:             the export type
          - enabled:          whether the export is enabled
          - executing:        whether it is currently running
          - is_queued:        whether it is queued for execution
          - started:          last start timestamp
          - completed:        last completion timestamp
          - last_export_status: whether the last run succeeded (bool)
          - next_schedule:    next scheduled run time
          - schedules:        list of schedule entries with their configuration
          - specification:    full specification dict (destination, filters, etc.)
    """
    response = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})

    register = response.get("Register") or {}
    schedules = response.get("Schedules") or []
    spec = response.get("Specification") or {}

    return {
        "id": register.get("Id"),
        "friendly_name": register.get("FriendlyName"),
        "type": register.get("Type"),
        "enabled": register.get("Enabled"),
        "executing": register.get("Executing"),
        "is_queued": register.get("IsQueued"),
        "started": register.get("Started"),
        "completed": register.get("Completed"),
        "last_export_status": register.get("LastExportStatus"),
        "next_schedule": register.get("NextSchedule"),
        "all_schedules_disabled": register.get("AllSchedulesDisabled"),
        "schedules": [
            {
                "schedule_id": s.get("Id"),
                "name": s.get("Name"),
                "schedule_xml": s.get("ScheduleXML"),
                "configuration": s.get("Configuration"),
            }
            for s in schedules
        ],
        "specification": spec,
    }


@_tool()
def run_import(import_id: int, dry_run: bool = True) -> dict:
    """
    Trigger an existing Linnworks import profile to run immediately.

    Puts the import into the queue — Linnworks picks it up within seconds.
    Use this to manually fire an import that would otherwise wait for its
    next scheduled run, or to run an import on demand (e.g. after uploading
    a new supplier catalogue CSV).

    Always reads the import configuration first so you can confirm exactly
    which import will run, what feed URL it reads from, and what type of
    Linnworks data it updates — before anything is triggered.

    Refuses to queue if the import is already executing or already queued,
    preventing accidental double-runs.

    Args:
        import_id: Integer ID of the import to trigger (from get_import_list).
        dry_run:   If True (default), shows the full import config and what
                   would be triggered — without actually queuing it.
                   Set to False to queue the import for immediate execution.

    Returns:
        A dict with:
          - dry_run:        whether this was a preview only
          - import_id:      the ID queried
          - friendly_name:  human-readable import name
          - type:           the import type (e.g. "Inventory", "StockLevel")
          - feed_url:       source file URL the import reads from
          - enabled:        whether the import is enabled in Linnworks
          - was_executing:  whether it was already running at read time
          - was_queued:     whether it was already queued at read time
          - queued:         True if successfully queued (live run only)
          - now_executing:  post-trigger executing state (live run only)
          - now_queued:     post-trigger queued state (live run only)
          - message:        human-readable status summary
    """
    response = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})
    register = response.get("Register") or {}
    spec     = response.get("Specification") or {}
    feed     = spec.get("Feed") or {}

    friendly_name = register.get("FriendlyName") or f"Import {import_id}"
    import_type   = register.get("Type")
    enabled       = register.get("Enabled", False)
    was_executing = register.get("Executing", False)
    was_queued    = register.get("IsQueued", False)
    feed_url      = feed.get("Url") or feed.get("FileUrl") or feed.get("FeedUrl")

    base = {
        "import_id":     import_id,
        "friendly_name": friendly_name,
        "type":          import_type,
        "feed_url":      feed_url,
        "enabled":       enabled,
        "was_executing": was_executing,
        "was_queued":    was_queued,
    }

    if was_executing:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Import '{friendly_name}' is already executing — "
                f"not queued again to avoid a double-run."
            ),
        }

    if was_queued:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Import '{friendly_name}' is already queued — "
                f"not queued again to avoid a double-run."
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "queued": False,
            "message": (
                f"Dry run — would queue import '{friendly_name}' "
                f"(type: {import_type}, feed: {feed_url or 'not configured'}). "
                f"Set dry_run=False to trigger."
            ),
        }

    started_before = (register or {}).get("Started")

    call_linnworks_void("ImportExport/RunNowImport", {"importId": import_id})

    readback    = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})
    rb_register = readback.get("Register") or {}
    now_queued    = rb_register.get("IsQueued", False)
    now_executing = rb_register.get("Executing", False)

    return {
        **base,
        "dry_run":       False,
        "queued":        now_queued or now_executing,
        "now_executing": now_executing,
        "now_queued":    now_queued,
        "started_before": started_before,
        "how_to_verify": (
            "Call get_import_list() again in ~15-30s and compare this import's "
            f"`started` against started_before ({started_before}). A NEW `started` "
            "timestamp is the proof it ran. Do NOT rely on `import_status` — it is "
            "null even for erroring imports (long-standing tenant quirk)."
        ),
        "message": (
            f"Import '{friendly_name}' triggered (RunNowImport accepted, HTTP 204). "
            + (
                "Read-back already shows it queued/executing."
                if (now_queued or now_executing)
                else (
                    "Read-back shows not queued yet — this is EXPECTED and is not a "
                    "failure; the job is usually picked up within seconds."
                )
            )
            + " Acceptance is not execution: verify via the `started` timestamp, "
              "per how_to_verify. ⚠️ An import WRITES to your catalogue — confirm "
              "what it changed."
        ),
    }


_RO_QUERY_DEFAULT_MAX_ROWS = 5000

_RO_QUERY_DENIED_WORDS = (
    "INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE", "DROP", "ALTER",
    "TRUNCATE", "CREATE", "GRANT", "REVOKE", "DENY", "INTO", "DECLARE", "WAITFOR",
    "OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "BULK", "SET", "USE",
    "DISABLE", "ENABLE", "UPDATETEXT", "WRITETEXT", "SHUTDOWN", "DBCC", "BACKUP",
    "RESTORE", "KILL", "RECONFIGURE", "CHECKPOINT", "BEGIN", "COMMIT", "ROLLBACK",
    "SAVE", "SETUSER", "REVERT", "SEND", "RECEIVE", "MOVE", "OPEN", "CLOSE", "FETCH",
    "DEALLOCATE",
)
_RO_QUERY_DENIED_RE = re.compile(
    r"\b(" + "|".join(_RO_QUERY_DENIED_WORDS) + r")\b", re.IGNORECASE
)
_RO_QUERY_PROC_RE = re.compile(r"\b(sp|xp)_\w*", re.IGNORECASE)
_RO_QUERY_START_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)


def _validate_readonly_sql(sql) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(sql, str) or not sql.strip():
        return None, "sql must be a non-empty string."
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        return None, "sql must be a non-empty string."
    if "--" in text or "/*" in text or "*/" in text:
        return None, (
            "SQL comments ('--', '/* */') are refused: a comment can hide a second "
            "statement from the guard. Remove the comment."
        )
    if ";" in text:
        return None, (
            "Only a single statement is allowed: a ';' appears before the end of the "
            "text (this also rejects a ';' inside a string literal)."
        )
    if not _RO_QUERY_START_RE.match(text):
        return None, "Only a statement that starts with SELECT or WITH is allowed."
    hit = _RO_QUERY_DENIED_RE.search(text)
    if hit:
        return None, (
            f"The word '{hit.group(1).upper()}' is refused: this tool is read-only. "
            "The whole text is scanned, string literals included, so a literal such "
            "as LIKE '%update%' is refused too — rephrase it."
        )
    proc = _RO_QUERY_PROC_RE.search(text)
    if proc:
        return None, (
            f"'{proc.group(0)}' is refused: sp_/xp_ procedures are not allowed."
        )
    return text, None


@_tool()
def run_readonly_query(sql: str, max_rows: int = _RO_QUERY_DEFAULT_MAX_ROWS) -> dict:
    """
    Run ONE read-only SQL query against the Linnworks database (Query Data /
    Dashboards/ExecuteCustomScriptQuery) to answer a catalogue-wide question in a
    single call, e.g. "which items have Taxable = 'False'?".

    READ-ONLY. There is no dry_run and no confirmation step. A strict CLIENT-SIDE
    guard allows one SELECT or WITH statement and refuses the patterns listed below
    before any request is sent. It is a best-effort guard (SELECT-first plus
    whole-word scanning), NOT a proof of safety. (_check_injection is deliberately
    not used: it targets free-text write fields, not SQL.) ⚠️ That guard is the only control known: whether the
    endpoint itself uses a read-only connection is unknown, so do not rely on the
    server to catch anything the guard lets through.

    Guard rules: the text must start with SELECT or WITH; one trailing ';' is
    stripped, any other ';' is refused; SQL comments ('--', '/* */') are refused;
    these whole words are refused anywhere in the text: INSERT UPDATE DELETE MERGE
    EXEC EXECUTE DROP ALTER TRUNCATE CREATE GRANT REVOKE DENY INTO DECLARE WAITFOR
    OPENROWSET OPENQUERY OPENDATASOURCE BULK SET USE DISABLE ENABLE UPDATETEXT WRITETEXT SHUTDOWN DBCC BACKUP RESTORE KILL RECONFIGURE CHECKPOINT BEGIN COMMIT ROLLBACK SAVE SETUSER REVERT SEND RECEIVE MOVE OPEN CLOSE FETCH DEALLOCATE, plus sp_ / xp_ names.
    NB: string literals are scanned too, so LIKE '%update%' is refused. Column names that merely
    contain a word (LastUpdateDate, CreatedBy) are fine.

    Known tables/columns (confirmed live): StockItem (ItemNumber, ItemTitle,
    pkStockItemID, bLogicalDelete, ...), StockItem_ExtendedProperties
    (fkStockItemId, ProperyName, ProperyValue, ProperyType), ProductCategories.
    ⚠️ The extended-property columns carry Linnworks' own typo: ProperyName /
    ProperyValue / ProperyType (one 't'), not Property*.

    Example, items and their 'Taxable' extended property:
      SELECT s.ItemNumber, e.ProperyValue, s.bLogicalDelete
      FROM StockItem_ExtendedProperties e
      JOIN StockItem s ON s.pkStockItemID = e.fkStockItemId
      WHERE e.ProperyName = 'Taxable'

    ⚠️ Default collation is case-INSENSITIVE, so 'FALSE' and 'False' group together
    and an inconsistent-casing hunt under-counts. Use COLLATE Latin1_General_CS_AS in
    BOTH the select list and the GROUP BY:
      SELECT ProperyValue COLLATE Latin1_General_CS_AS AS v, COUNT(*) AS n
      FROM StockItem_ExtendedProperties WHERE ProperyName = 'Taxable'
      GROUP BY ProperyValue COLLATE Latin1_General_CS_AS

    ⚠️ Rows with bLogicalDelete = 1 (deleted/archived items) ARE returned, but they
    cannot be resolved by SKU (GetInventoryItem 400s on them) and cannot be targeted
    by any write tool such as set_extended_properties or delete_extended_properties —
    those will report per-item 'not found'. Select bLogicalDelete and filter on it.

    max_rows (default 5000, must be a positive integer) caps the rows returned inline
    to protect conversation context; lower it for wide rows.

    Returns, on success: columns (names, in order), rows (list of dicts, at most
    max_rows), total_results (Linnworks' TotalResults), row_count, truncated (True
    when rows were cut to max_rows OR TotalResults exceeds the rows Linnworks
    returned). A SQL error comes back as HTTP 200 with IsError and is returned as
    {"error": <ErrorMessage verbatim>, "is_error": True} with NO rows key — never as
    an empty result. A refused statement returns {"refused": True, "error": reason}
    and makes no HTTP call. A quota failure (HTTP 429) is retried on the 5/10/20/30s
    ladder and then raises RateLimitError — it is never reported as a SQL error or as
    zero rows.
    """
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        return {
            "refused": True,
            "error": f"max_rows must be a positive integer, got {max_rows!r}.",
        }

    clean, reason = _validate_readonly_sql(sql)
    if reason:
        return {"refused": True, "error": reason}

    response = call_linnworks_form(
        "Dashboards/ExecuteCustomScriptQuery",
        {"script": clean},
        retry_rate_limit=True,
    )

    if not isinstance(response, dict):
        return {
            "is_error": True,
            "error": f"Unexpected response shape from Linnworks: {type(response).__name__}",
        }
    if response.get("IsError"):
        return {"is_error": True, "error": response.get("ErrorMessage")}

    all_rows = response.get("Results") or []
    columns = [c.get("Name") for c in (response.get("Columns") or [])]
    rows = list(all_rows[:max_rows])
    total = response.get("TotalResults")
    cut = len(all_rows) > max_rows
    short = isinstance(total, int) and total > len(all_rows)
    return {
        "columns": columns,
        "rows": rows,
        "total_results": total,
        "row_count": len(rows),
        "truncated": bool(cut or short),
    }


@_tool()
def run_export(export_id: int, dry_run: bool = True) -> dict:
    """
    Trigger an existing Linnworks export profile to run immediately.

    Puts the export into the queue — Linnworks picks it up within seconds.
    Use this to manually fire an export that would otherwise wait for its
    next scheduled run, or to produce an on-demand data extract.

    Always reads the export configuration first so you can confirm exactly
    which export will run and what it produces — before anything is triggered.

    Refuses to queue if the export is already executing or already queued,
    preventing accidental double-runs.

    Args:
        export_id: Integer ID of the export to trigger (from get_export_list).
        dry_run:   If True (default), shows the export config and what would
                   be triggered — without actually queuing it.
                   Set to False to queue the export for immediate execution.

    Returns:
        A dict with:
          - dry_run:        whether this was a preview only
          - export_id:      the ID queried
          - friendly_name:  human-readable export name
          - type:           the export type
          - enabled:        whether the export is enabled in Linnworks
          - was_executing:  whether it was already running at read time
          - was_queued:     whether it was already queued at read time
          - queued:         True if successfully queued (live run only)
          - now_executing:  post-trigger executing state (live run only)
          - now_queued:     post-trigger queued state (live run only)
          - message:        human-readable status summary
    """
    response = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})
    register = response.get("Register") or {}

    friendly_name = register.get("FriendlyName") or f"Export {export_id}"
    export_type   = register.get("Type")
    enabled       = register.get("Enabled", False)
    was_executing = register.get("Executing", False)
    was_queued    = register.get("IsQueued", False)

    base = {
        "export_id":     export_id,
        "friendly_name": friendly_name,
        "type":          export_type,
        "enabled":       enabled,
        "was_executing": was_executing,
        "was_queued":    was_queued,
    }

    if was_executing:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Export '{friendly_name}' is already executing — "
                f"not queued again to avoid a double-run."
            ),
        }

    if was_queued:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Export '{friendly_name}' is already queued — "
                f"not queued again to avoid a double-run."
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "queued": False,
            "message": (
                f"Dry run — would queue export '{friendly_name}' "
                f"(type: {export_type}). "
                f"Set dry_run=False to trigger."
            ),
        }

    started_before = (register or {}).get("Started")

    call_linnworks_void("ImportExport/RunNowExport", {"exportId": export_id})

    readback    = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})
    rb_register = readback.get("Register") or {}
    now_queued    = rb_register.get("IsQueued", False)
    now_executing = rb_register.get("Executing", False)

    return {
        **base,
        "dry_run":       False,
        "queued":        now_queued or now_executing,
        "now_executing": now_executing,
        "now_queued":    now_queued,
        "started_before": started_before,
        "how_to_verify": (
            "Call get_export_list() again in ~15-30s and compare this export's "
            f"`started` against started_before ({started_before}). A NEW `started` "
            "timestamp is the proof it ran. Do NOT read `last_export_status` as "
            "success: it is False on every export in this account, including "
            "production ones that ran correctly on schedule today — it is a "
            "constant, not a result."
        ),
        "message": (
            f"Export '{friendly_name}' triggered (RunNowExport accepted, HTTP 204). "
            + (
                f"Read-back already shows it queued/executing."
                if (now_queued or now_executing)
                else (
                    "Read-back shows not queued yet — this is EXPECTED and is not a "
                    "failure; the job is usually picked up within seconds."
                )
            )
            + " Acceptance is not execution: verify via the `started` timestamp, "
              "per how_to_verify."
        ),
    }


def _resolve_sku_to_id(sku: str, cache: dict | None = None) -> str:
    if cache is not None and sku in cache:
        return cache[sku]
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError:
        raise
    except RuntimeError as exc:
        raise ValueError(f"SKU '{sku}' not found in Linnworks: {exc}") from exc
    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        raise ValueError(f"SKU '{sku}' was found but returned no StockItemId.")
    if cache is not None:
        cache[sku] = stock_item_id
    return stock_item_id


def _resolve_category(entry: dict, cache: dict) -> tuple[str, str] | None:
    name = (entry.get("category_name") or "").strip()
    cid  = (entry.get("category_id") or "").strip()
    if not name and not cid:
        return None

    if "categories" not in cache:
        cache["categories"] = _fetch_categories()
    cats = cache["categories"]

    by_name = {(c.get("CategoryName") or "").strip().lower(): c for c in cats}
    by_id   = {(c.get("CategoryId") or "").lower(): c for c in cats}
    known   = ", ".join(sorted(c.get("CategoryName") or "" for c in cats))

    rec_by_id   = by_id.get(cid.lower()) if cid else None
    rec_by_name = by_name.get(name.lower()) if name else None

    if cid and rec_by_id is None:
        raise ValueError(
            f"category_id '{cid}' does not match any Linnworks category. "
            f"Use get_categories to list valid ids. Known names: {known}."
        )
    if name and rec_by_name is None:
        raise ValueError(
            f"category_name '{name}' does not match any Linnworks category "
            f"(matched case-insensitively). Use get_categories to list them. "
            f"Known names: {known}."
        )
    if (rec_by_id and rec_by_name
            and (rec_by_id.get("CategoryId") or "").lower()
                != (rec_by_name.get("CategoryId") or "").lower()):
        raise ValueError(
            f"category_id '{cid}' is '{rec_by_id.get('CategoryName')}' but "
            f"category_name '{name}' is '{rec_by_name.get('CategoryName')}' "
            f"({rec_by_name.get('CategoryId')}) — pass one, or make them agree."
        )

    rec = rec_by_id or rec_by_name
    return rec.get("CategoryId"), rec.get("CategoryName")


def _read_back_category(sku: str, requested: tuple[str, str] | None,
                        category_cache: dict | None = None) -> dict:
    try:
        fresh = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError:
        return {"category_id": None, "category_name": None,
                "category_verified": None, "readback": "rate_limited"}
    except Exception as exc:
        return {"category_id": None, "category_name": None,
                "category_verified": None, "readback": f"failed: {exc}"}

    got_id   = fresh.get("CategoryId")
    got_name = fresh.get("CategoryName")
    if not got_name and got_id and category_cache:
        names_by_id = {(c.get("CategoryId") or "").lower(): c.get("CategoryName")
                       for c in (category_cache.get("categories") or [])}
        got_name = names_by_id.get(got_id.lower())

    out = {
        "category_id":   got_id,
        "category_name": got_name,
        "readback":      "ok",
    }
    if requested is None:
        out["category_verified"] = None
        return out

    want_id, want_name = requested
    landed = (got_id or "").lower() == (want_id or "").lower()
    out["category_verified"] = landed
    if not landed:
        out["warning"] = (
            f"Category did not land: asked for '{want_name}' ({want_id}) but "
            f"Linnworks read back '{got_name}' ({got_id}). "
            f"The item write itself was accepted."
        )
    return out


@_tool()
def create_or_update_inventory_item(
    items: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update inventory items, matched by SKU: an existing SKU is updated,
    a new one is created. On update only the fields you pass change; the rest of
    the record is read first and carried through, because Linnworks clears any
    field left out of an update.

    Category: Linnworks keys the category on its GUID and ignores a name sent on
    its own. category_name (case-insensitive) or category_id is resolved against
    Inventory/GetCategories, and both are sent. The category must already exist
    (see get_categories). An unknown one is a per-item error: `category_error` in
    the manifest, and that item is skipped on a live run while the rest proceed.

    Every live write is read back. `category_verified` (True/False, None if the
    read-back was rate-limited) is the field to trust: a 2xx alone has not proved
    a category landed. Mismatches are counted in `category_mismatches` with a
    `warning`. `category_name` in the read-back comes from the GetCategories list
    (Linnworks returns it null) and stays null if nothing in the batch named a
    category.

    Batches over 50 items stage: confirm with confirmed_count=<N>.

    Args:
        items: List of dicts. Required: "sku". Optional: title, barcode,
            retail_price, purchase_price, tax_rate (e.g. 20.0), category_name,
            category_id (GUID; must agree with category_name if both given),
            weight (kg), height, width, depth (cm), metadata.
        confirmed_count: For batches over 50, pass len(items).
        dry_run: Default True: returns the manifest and writes nothing.

    Returns:
        dry_run, item_count, manifest (per item, with the resolved category_id or
        category_error), category_errors; and on a live run: results (with the
        read-back category_id, category_name, category_verified, readback),
        created, updated, errors, category_mismatches and warning.
    """
    for entry in items:
        _check_injection("title",     entry.get("title", ""))
        _check_injection("barcode",   entry.get("barcode", ""))
        _check_injection("metadata",  entry.get("metadata", ""))

    category_cache: dict = {}
    resolved_categories: list = []
    for entry in items:
        try:
            resolved_categories.append(_resolve_category(entry, category_cache))
        except ValueError as exc:
            resolved_categories.append(exc)

    manifest = []
    for entry, cat in zip(items, resolved_categories):
        resolved = isinstance(cat, tuple)
        row = {
            "sku":            entry.get("sku"),
            "title":          entry.get("title"),
            "barcode":        entry.get("barcode"),
            "retail_price":   entry.get("retail_price"),
            "purchase_price": entry.get("purchase_price"),
            "tax_rate":       entry.get("tax_rate"),
            "category_name":  cat[1] if resolved else entry.get("category_name"),
            "category_id":    cat[0] if resolved else None,
            "weight":         entry.get("weight"),
            "height":         entry.get("height"),
            "width":          entry.get("width"),
            "depth":          entry.get("depth"),
            "metadata":       entry.get("metadata"),
        }
        if isinstance(cat, ValueError):
            row["category_error"] = str(cat)
        manifest.append(row)
    category_errors = sum(1 for c in resolved_categories if isinstance(c, ValueError))

    guard = _write_guard("create_or_update_inventory_item", items, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "category_errors": category_errors}

    if dry_run:
        message = "Dry run — no changes written. Set dry_run=False to execute."
        if category_errors:
            message += (
                f" {category_errors} item(s) name a category that does not exist "
                f"(see category_error) — they would be skipped on a live run."
            )
        return {
            "dry_run": True,
            "item_count": len(items),
            "manifest": manifest,
            "category_errors": category_errors,
            "message": message,
        }

    sku_cache: dict[str, str] = {}
    results = []
    created = 0
    updated = 0
    errors  = 0
    category_mismatches = 0

    for entry, cat in zip(items, resolved_categories):
        sku = (entry.get("sku") or "").strip()
        if not sku:
            results.append({"sku": "", "action": "error", "error": "Missing 'sku' field."})
            errors += 1
            continue
        if isinstance(cat, ValueError):
            results.append({"sku": sku, "action": "error", "error": str(cat)})
            errors += 1
            continue

        try:
            try:
                existing = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
                stock_item_id = existing.get("StockItemId")
                action = "update"
            except RuntimeError:
                existing = None
                stock_item_id = None
                action = "create"

            if action == "update":
                item_payload = {
                    "StockItemId":            stock_item_id,
                    "ItemNumber":             existing.get("ItemNumber", sku),
                    "ItemTitle":              entry.get("title")          or existing.get("ItemTitle", ""),
                    "BarcodeNumber":          entry.get("barcode")        or existing.get("BarcodeNumber", ""),
                    "RetailPrice":            entry.get("retail_price")   if entry.get("retail_price") is not None else existing.get("RetailPrice"),
                    "PurchasePrice":          entry.get("purchase_price") if entry.get("purchase_price") is not None else existing.get("PurchasePrice"),
                    "TaxRate":                entry.get("tax_rate")       if entry.get("tax_rate") is not None else existing.get("TaxRate"),
                    "CategoryName":           cat[1] if cat else existing.get("CategoryName", ""),
                    "CategoryId":             cat[0] if cat else existing.get("CategoryId", ""),
                    "Weight":                 entry.get("weight")         if entry.get("weight") is not None else existing.get("Weight"),
                    "Height":                 entry.get("height")         if entry.get("height") is not None else existing.get("Height"),
                    "Width":                  entry.get("width")          if entry.get("width") is not None else existing.get("Width"),
                    "Depth":                  entry.get("depth")          if entry.get("depth") is not None else existing.get("Depth"),
                    "MetaData":               entry.get("metadata")       or existing.get("MetaData", ""),
                    "IsCompositeParent":      existing.get("IsCompositeParent", False),
                    "ShippedSeparately":      existing.get("ShippedSeparately", False),
                    "IsVariationParent":      existing.get("IsVariationParent", False),
                    "isBatchedStockType":     existing.get("isBatchedStockType", False),
                    "PostalServiceId":        existing.get("PostalServiceId", ""),
                    "PostalServiceName":      existing.get("PostalServiceName", ""),
                    "PackageGroupId":         existing.get("PackageGroupId", ""),
                    "PackageGroupName":       existing.get("PackageGroupName", ""),
                    "InventoryTrackingType":  existing.get("InventoryTrackingType", 0),
                    "BatchNumberScanRequired":existing.get("BatchNumberScanRequired", False),
                    "SerialNumberScanRequired":existing.get("SerialNumberScanRequired", False),
                }
                call_linnworks("Inventory/UpdateInventoryItem", {"inventoryItem": item_payload})
                updated += 1
                result = {
                    "sku": sku, "action": "updated", "stock_item_id": stock_item_id,
                    "title": item_payload["ItemTitle"],
                }

            else:
                new_guid = str(uuid.uuid4())
                item_payload = {
                    "StockItemId":   new_guid,
                    "ItemNumber":    sku,
                    "ItemTitle":     entry.get("title", ""),
                    "BarcodeNumber": entry.get("barcode", ""),
                    "RetailPrice":   entry.get("retail_price", 0.0),
                    "PurchasePrice": entry.get("purchase_price", 0.0),
                    "TaxRate":       entry.get("tax_rate", 0.0),
                    "CategoryName":  "",
                    "Weight":        entry.get("weight", 0.0),
                    "Height":        entry.get("height", 0.0),
                    "Width":         entry.get("width", 0.0),
                    "Depth":         entry.get("depth", 0.0),
                    "MetaData":      entry.get("metadata", ""),
                }
                if cat:
                    item_payload["CategoryId"], item_payload["CategoryName"] = cat
                resp = call_linnworks("Inventory/AddInventoryItem", {"inventoryItem": item_payload})
                new_id = (resp.get("fkStockItemId") if isinstance(resp, dict) else None) or new_guid
                created += 1
                result = {
                    "sku": sku, "action": "created", "stock_item_id": new_id,
                    "title": item_payload["ItemTitle"],
                }

            result.update(_read_back_category(sku, cat, category_cache))
            if result.get("category_verified") is False:
                category_mismatches += 1
            results.append(result)

        except Exception as exc:
            errors += 1
            results.append({"sku": sku, "action": "error", "error": str(exc)})

    out = {
        "dry_run":             False,
        "item_count":          len(items),
        "created":             created,
        "updated":             updated,
        "errors":              errors,
        "category_mismatches": category_mismatches,
        "results":             results,
    }
    if category_mismatches:
        out["warning"] = (
            f"{category_mismatches} item(s) were written but the requested category "
            f"did not land on read-back (see per-item warning / category_id). "
            f"Linnworks accepted the write; check the item in get_categories / "
            f"search_inventory_items before relying on it."
        )
    return out


@_tool()
def set_stock_levels(
    updates: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Set absolute stock levels for one or more SKUs in Linnworks.

    WARNING: This overwrites live stock figures immediately and affects
    channel availability (Amazon, eBay, Shopify) in near-real-time.
    Always use dry_run=True first to review the changes.

    For batches larger than 25 items this tool enters a staging mode: it
    returns a manifest showing current → new stock levels and asks you to
    confirm with confirmed_count=<N> before executing.

    Uses Stock/UpdateStockLevelsBulk which returns per-item errors so every
    line can be individually reported.

    Args:
        updates: List of dicts, each with:
            - sku (str):         The item's SKU / ItemNumber  [required]
            - stock_level (int): The new absolute stock level  [required]
            - location_id (str): Linnworks location UUID.
                                 Defaults to the Default location.
        confirmed_count: For batches > 25, pass len(updates) here after
            reviewing the manifest to confirm the write.
        dry_run: If True (default), returns a before/after manifest without
            writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of SKUs in the batch
          - manifest:    per-item before/after preview (always present)
          - results:     per-item outcome with errors (live run only)
          - errors:      count of SKUs that failed (live run only)
    """
    skus = [(u.get("sku") or "").strip() for u in updates]
    for i, s in enumerate(skus):
        if not s:
            raise ValueError(f"updates[{i}] is missing 'sku'.")
    for u in updates:
        if u.get("stock_level") is None:
            raise ValueError(f"updates entry for SKU '{u.get('sku')}' is missing 'stock_level'.")

    sku_cache: dict[str, str] = {}
    current_levels: dict[str, int | None] = {}

    for sku in skus:
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
            levels_resp = call_linnworks(
                "Stock/GetStockLevel_Batch",
                {"request": {"StockItemIds": [stock_item_id]}},
            )
            rows = levels_resp if isinstance(levels_resp, list) else (levels_resp.get("StockItemLevels") or [])
            for u in updates:
                if (u.get("sku") or "").strip() == sku:
                    target_loc = u.get("location_id", DEFAULT_LOCATION_ID)
            loc_level = None
            for row_group in rows:
                for row in (row_group if isinstance(row_group, list) else [row_group]):
                    if row.get("Location", {}).get("StockLocationId") == target_loc:
                        loc_level = row.get("StockLevel")
                        break
            current_levels[sku] = loc_level
        except (ValueError, RuntimeError):
            current_levels[sku] = None

    manifest = []
    for u in updates:
        sku = (u.get("sku") or "").strip()
        new_level = u.get("stock_level")
        loc_id = u.get("location_id", DEFAULT_LOCATION_ID)
        current = current_levels.get(sku)
        delta = None if current is None else (new_level - current)
        manifest.append({
            "sku":              sku,
            "location_id":      loc_id,
            "current_level":    current,
            "new_level":        new_level,
            "delta":            delta,
        })

    guard = _write_guard("set_stock_levels", updates, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":     True,
            "item_count":  len(updates),
            "manifest":    manifest,
            "message": (
                "Dry run — no stock levels changed. "
                "Review the manifest and set dry_run=False to execute."
            ),
        }

    bulk_items = []
    for u in updates:
        sku = (u.get("sku") or "").strip()
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError:
            stock_item_id = ""
        bulk_items.append({
            "SKU":             sku,
            "StockItemId":     stock_item_id,
            "StockLocationId": u.get("location_id", DEFAULT_LOCATION_ID),
            "StockLevel":      u.get("stock_level"),
        })

    response = call_linnworks("Stock/UpdateStockLevelsBulk", {"Items": bulk_items})
    response_items = response.get("Items") or []

    results = []
    errors = 0

    if not response_items:
        for bi in bulk_items:
            results.append({
                "sku":         bi.get("SKU"),
                "location_id": bi.get("StockLocationId"),
                "new_level":   bi.get("StockLevel"),
                "success":     True,
                "errors":      [],
                "note":        "API returned no content; success inferred from 2xx. Verify with get_stock_level.",
            })
        return {
            "dry_run":    False,
            "item_count": len(updates),
            "errors":     errors,
            "results":    results,
            "manifest":   manifest,
        }

    for idx, item in enumerate(response_items):
        errs = item.get("Errors") or []
        ok = len(errs) == 0
        if not ok:
            errors += 1
        results.append({
            "sku":         item.get("SKU"),
            "location_id": item.get("StockLocationId"),
            "new_level":   item.get("StockLevel"),
            "success":     ok,
            "errors":      errs,
        })

    return {
        "dry_run":    False,
        "item_count": len(updates),
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def set_inventory_item_prices(
    prices: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Set or update channel prices for one or more inventory items in Linnworks.

    Prices in Linnworks are keyed by (StockItemId, Source, SubSource).  This
    tool reads existing price rows for each SKU, then creates or updates rows
    as needed.  Pass source="" and sub_source="" to set the default price row.

    WARNING: Price changes are pushed to connected sales channels
    (Amazon, eBay, Shopify) in near-real-time.  Always use dry_run=True
    first to review the manifest.

    For batches larger than 25 items this tool requires confirmed_count=<N>.

    Args:
        prices: List of dicts, each with:
            - sku (str):         Item SKU  [required]
            - price (float):     The new price  [required]
            - source (str):      Channel source (e.g. "AMAZON", "EBAY", "").
                                 Defaults to "" (default price row).
            - sub_source (str):  Channel sub-source (e.g. "DEFAULT").
                                 Defaults to "".
        confirmed_count: For batches > 25, pass len(prices) here.
        dry_run: If True (default), returns a before/after manifest without
            writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of price rows in the batch
          - manifest:    per-item before/after preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    for i, p in enumerate(prices):
        if not p.get("sku"):
            raise ValueError(f"prices[{i}] is missing 'sku'.")
        if p.get("price") is None:
            raise ValueError(f"prices[{i}] (SKU '{p.get('sku')}') is missing 'price'.")

    sku_cache: dict[str, str] = {}

    manifest = []
    existing_prices: dict[str, list] = {}

    for p in prices:
        sku        = p["sku"].strip()
        source     = p.get("source", "")
        sub_source = p.get("sub_source", "")
        new_price  = p["price"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "new_price": new_price, "current_price": None, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_prices:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemPrices",
                params={"inventoryItemId": stock_item_id},
            )
            existing_prices[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_prices[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        if match:
            action, pk_row_id = "update", match.get("pkRowId")
        elif source == "" and sub_source == "":
            action, pk_row_id = "update", "00000000-0000-0000-0000-000000000000"
        else:
            action, pk_row_id = "create", None
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "source":        source,
            "sub_source":    sub_source,
            "current_price": match.get("Price") if match else None,
            "new_price":     new_price,
            "action":        action,
            "pk_row_id":     pk_row_id,
        })

    guard = _write_guard("set_inventory_item_prices", prices, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(prices),
            "manifest":   manifest,
            "message":    "Dry run — no prices changed. Set dry_run=False to execute.",
        }

    to_create = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results   = []
    created   = 0
    updated   = 0
    errors    = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Price":       m["new_price"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemPrices",
            {"inventoryItemPrices": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
                "new_price": m["new_price"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Price":       m["new_price"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemPrices",
            {"inventoryItemPrices": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_price": m["current_price"], "new_price": m["new_price"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(prices),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def set_extended_properties(
    properties: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update extended properties on Linnworks inventory items.

    Extended properties are key/value metadata pairs attached to a stock item
    (e.g. "Colour": "Red", "Material": "Maple").  This tool upserts each
    property: if a property with the given name already exists on the item it
    is updated; if not, it is created.

    Note: The Linnworks API has a confirmed typo in the field name — "ProperyName"
    (one 't') — which this tool handles transparently.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        properties: List of dicts, each with:
            - sku (str):            Item SKU  [required]
            - property_name (str):  Name of the extended property  [required]
            - property_value (str): Value to set  [required]
            - property_type (str):  Property type label (e.g. "Attribute",
                                    "Specification"). Defaults to "Attribute".
        confirmed_count: For batches > 50, pass len(properties) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of property rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new properties created (live run only)
          - updated:     existing properties updated (live run only)
          - errors:      rows that failed (live run only)
    """
    for p in properties:
        _check_injection("property_name",  p.get("property_name", ""))
        _check_injection("property_value", p.get("property_value", ""))

    for i, p in enumerate(properties):
        if not p.get("sku"):
            raise ValueError(f"properties[{i}] is missing 'sku'.")
        if not p.get("property_name"):
            raise ValueError(f"properties[{i}] (SKU '{p.get('sku')}') is missing 'property_name'.")
        if p.get("property_value") is None:
            raise ValueError(f"properties[{i}] (SKU '{p.get('sku')}') is missing 'property_value'.")

    sku_cache: dict[str, str] = {}
    existing_props: dict[str, list] = {}

    manifest = []
    for p in properties:
        sku      = p["sku"].strip()
        prop_name = p["property_name"]
        prop_val  = str(p["property_value"])
        prop_type = p.get("property_type", "Attribute")

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "property_name": prop_name,
                "property_value": prop_val, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_props:
            existing_props[stock_item_id] = _fetch_extended_properties(stock_item_id)

        match = next(
            (r for r in existing_props[stock_item_id] if r.get("ProperyName") == prop_name),
            None,
        )
        manifest.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "property_name":   prop_name,
            "old_value":       match.get("PropertyValue") if match else None,
            "new_value":       prop_val,
            "property_type":   prop_type,
            "action":          "update" if match else "create",
            "pk_row_id":       match.get("pkRowId") if match else None,
        })

    guard = _write_guard("set_extended_properties", properties, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(properties),
            "manifest":   manifest,
            "message":    "Dry run — no properties changed. Set dry_run=False to execute.",
        }

    to_create = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":       str(uuid.uuid4()),
                "fkStockItemId": m["stock_item_id"],
                "SKU":           m["sku"],
                "ProperyName":   m["property_name"],
                "PropertyValue": m["new_value"],
                "PropertyType":  m["property_type"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemExtendedProperties",
            {"inventoryItemExtendedProperties": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "property_name": m["property_name"], "value": m["new_value"],
            })

    if to_update:
        update_payload = [
            {
                "fkStockItemId": m["stock_item_id"],
                "pkRowId":       m["pk_row_id"],
                "ProperyName":   m["property_name"],
                "PropertyValue": m["new_value"],
                "PropertyType":  m["property_type"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemExtendedProperties",
            {"inventoryItemExtendedProperties": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "property_name": m["property_name"],
                "old_value": m["old_value"], "new_value": m["new_value"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(properties),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def delete_extended_properties(
    properties: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Delete extended-property rows from inventory items: the delete counterpart to
    set_extended_properties, which can only add or update.

    Rows are matched by (sku, property_name), EXACT and CASE-SENSITIVE; a name in
    a different case doesn't match and is reported, never guessed. Pass
    `expected_value` to delete only if the current value matches; otherwise the
    row is BLOCKED. If an item has several rows with the same name, ALL of them
    are deleted, and the manifest lists every row_id.

    ⚠️ IRREVERSIBLE. To restore a row, set it again with set_extended_properties
    using the value shown in the manifest. Staging counts the ROWS to be deleted
    after matching, not the input entries: over 10 needs confirmed_count=<that
    number>.

    The current properties are read first, so the manifest shows real values and
    row ids. After a live delete they are re-read fresh, and every row id is
    reported as `deleted` or `still_present`. A rate limit is reported under
    `rate_limited` with `complete: False`, never as "not found".

    Args:
        properties: List of dicts with sku and property_name (both required) and
            an optional expected_value.
        confirmed_count: When over 10 rows would be deleted, that resolved count.
        dry_run: Default True: returns the manifest and deletes nothing.

    Returns:
        dry_run, item_count, to_delete_count, manifest (sku, stock_item_id,
        property_name, expected_value, current_value, row_id, status, action,
        reason), rate_limited, complete; and on a live run: deleted and
        still_present.
    """
    for p in properties:
        _check_injection("property_name", p.get("property_name", ""))
        if p.get("expected_value") is not None:
            _check_injection("expected_value", p.get("expected_value"))

    for i, p in enumerate(properties):
        if not p.get("sku"):
            raise ValueError(f"properties[{i}] is missing 'sku'.")
        if not p.get("property_name"):
            raise ValueError(f"properties[{i}] (SKU '{p.get('sku')}') is missing 'property_name'.")

    sku_cache: dict[str, str] = {}
    existing_props: dict[str, list] = {}

    manifest: list[dict] = []
    rate_limited: list[dict] = []
    had_rate_limit = False

    for p in properties:
        sku = p["sku"].strip()
        prop_name = p["property_name"]
        expected_value = p.get("expected_value")

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except RateLimitError as exc:
            had_rate_limit = True
            rate_limited.append({
                "sku": sku, "property_name": prop_name,
                "reason": f"Rate-limited while resolving SKU '{sku}': {exc}",
            })
            continue
        except ValueError as exc:
            manifest.append({
                "sku": sku, "stock_item_id": None, "property_name": prop_name,
                "expected_value": expected_value, "current_value": None,
                "row_id": None, "status": "unresolved", "action": None,
                "reason": str(exc),
            })
            continue

        if stock_item_id not in existing_props:
            try:
                existing_props[stock_item_id] = _fetch_extended_properties(stock_item_id)
            except RateLimitError as exc:
                had_rate_limit = True
                rate_limited.append({
                    "sku": sku, "property_name": prop_name,
                    "reason": f"Rate-limited while reading properties for SKU '{sku}': {exc}",
                })
                continue

        matches = [
            r for r in existing_props[stock_item_id] if r.get("ProperyName") == prop_name
        ]

        if not matches:
            manifest.append({
                "sku": sku, "stock_item_id": stock_item_id, "property_name": prop_name,
                "expected_value": expected_value, "current_value": None,
                "row_id": None, "status": "unresolved", "action": None,
                "reason": f"Property '{prop_name}' not found on SKU '{sku}'.",
            })
            continue

        for match in matches:
            current_value = match.get("PropertyValue")
            row_id = match.get("pkRowId")
            if expected_value is not None and str(current_value) != str(expected_value):
                manifest.append({
                    "sku": sku, "stock_item_id": stock_item_id, "property_name": prop_name,
                    "expected_value": expected_value, "current_value": current_value,
                    "row_id": row_id, "status": "resolved", "action": "blocked",
                    "reason": (
                        f"expected_value '{expected_value}' does not match current "
                        f"value '{current_value}' for property '{prop_name}' on SKU '{sku}' "
                        f"(row {row_id}) — not deleted."
                    ),
                })
                continue
            manifest.append({
                "sku": sku, "stock_item_id": stock_item_id, "property_name": prop_name,
                "expected_value": expected_value, "current_value": current_value,
                "row_id": row_id, "status": "resolved", "action": "delete",
                "reason": None,
            })

    to_delete_rows = [m for m in manifest if m["action"] == "delete"]

    guard = _write_guard("delete_extended_properties", to_delete_rows, confirmed_count, dry_run)
    if guard is not None:
        return {
            **guard, "manifest": manifest, "rate_limited": rate_limited,
            "complete": not had_rate_limit,
        }

    if dry_run:
        return {
            "dry_run":         True,
            "item_count":      len(properties),
            "to_delete_count": len(to_delete_rows),
            "manifest":        manifest,
            "rate_limited":    rate_limited,
            "complete":        not had_rate_limit,
            "message": (
                f"Dry run — no properties deleted. {len(to_delete_rows)} row(s) would "
                f"be removed. Set dry_run=False to execute."
            ),
        }

    by_item: dict[str, list[str]] = {}
    for m in to_delete_rows:
        by_item.setdefault(m["stock_item_id"], []).append(m["row_id"])

    deleted: list[str] = []
    still_present: list[str] = []

    for stock_item_id, row_ids in by_item.items():
        try:
            call_linnworks_void(
                "Inventory/DeleteInventoryItemExtendedProperties",
                {
                    "inventoryItemId": stock_item_id,
                    "inventoryItemExtendedPropertyIds": row_ids,
                },
            )
        except RateLimitError as exc:
            had_rate_limit = True
            rate_limited.append({
                "stock_item_id": stock_item_id, "row_ids": row_ids,
                "reason": f"Rate-limited while deleting properties: {exc}",
            })
            continue

        try:
            fresh_rows = _fetch_extended_properties(stock_item_id)
        except RateLimitError as exc:
            had_rate_limit = True
            rate_limited.append({
                "stock_item_id": stock_item_id, "row_ids": row_ids,
                "reason": f"Rate-limited during read-back: {exc}",
            })
            continue

        remaining_ids = {r.get("pkRowId") for r in fresh_rows}
        for row_id in row_ids:
            if row_id in remaining_ids:
                still_present.append(row_id)
            else:
                deleted.append(row_id)

    return {
        "dry_run":         False,
        "item_count":      len(properties),
        "to_delete_count": len(to_delete_rows),
        "manifest":        manifest,
        "rate_limited":    rate_limited,
        "complete":        not had_rate_limit,
        "deleted":         deleted,
        "still_present":   still_present,
    }


@_tool()
def set_inventory_item_descriptions(
    descriptions: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update channel-specific descriptions on Linnworks inventory items.

    Descriptions in Linnworks are keyed by (StockItemId, Source, SubSource),
    allowing different description text per channel.  Pass source="" and
    sub_source="" to set the default description.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        descriptions: List of dicts, each with:
            - sku (str):         Item SKU  [required]
            - description (str): The description text  [required]
            - source (str):      Channel source (e.g. "AMAZON"). Defaults to "".
            - sub_source (str):  Channel sub-source. Defaults to "".
        confirmed_count: For batches > 50, pass len(descriptions) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of description rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    for d in descriptions:
        _check_injection("description", d.get("description", ""))

    for i, d in enumerate(descriptions):
        if not d.get("sku"):
            raise ValueError(f"descriptions[{i}] is missing 'sku'.")
        if d.get("description") is None:
            raise ValueError(f"descriptions[{i}] (SKU '{d.get('sku')}') is missing 'description'.")

    sku_cache: dict[str, str] = {}
    existing_descs: dict[str, list] = {}

    manifest = []
    for d in descriptions:
        sku        = d["sku"].strip()
        source     = d.get("source", "")
        sub_source = d.get("sub_source", "")
        new_desc   = d["description"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "description": new_desc, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_descs:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemDescriptions",
                params={"inventoryItemId": stock_item_id},
            )
            existing_descs[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_descs[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        manifest.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "source":          source,
            "sub_source":      sub_source,
            "old_description": match.get("Description") if match else None,
            "new_description": new_desc,
            "action":          "update" if match else "create",
            "pk_row_id":       match.get("pkRowId") if match else None,
        })

    guard = _write_guard("set_inventory_item_descriptions", descriptions, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(descriptions),
            "manifest":   manifest,
            "message":    "Dry run — no descriptions changed. Set dry_run=False to execute.",
        }

    to_create  = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update  = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Description": m["new_description"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemDescriptions",
            {"inventoryItemDescriptions": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Description": m["new_description"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemDescriptions",
            {"inventoryItemDescriptions": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_description": m["old_description"],
                "new_description": m["new_description"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(descriptions),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def get_inventory_item_descriptions(sku: str) -> dict:
    """
    Read the channel-specific descriptions for ONE inventory item.

    Descriptions in Linnworks are keyed by (StockItemId, Source, SubSource), so an
    item can carry a different description per channel plus a default (Source and
    SubSource both blank). This is the read-side companion to
    `set_inventory_item_descriptions` (which previously had no getter).

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - description_count: number of description rows
          - default_description: the text of the default row (blank Source +
            SubSource), or None if no default row exists
          - descriptions: per-row entries (source, sub_source, description,
            pk_row_id)
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemDescriptions",
        params={"inventoryItemId": stock_item_id},
    )
    descriptions = [
        {
            "source":      r.get("Source", ""),
            "sub_source":  r.get("SubSource", ""),
            "description": r.get("Description"),
            "pk_row_id":   r.get("pkRowId"),
        }
        for r in (rows if isinstance(rows, list) else [])
    ]
    default = next(
        (d["description"] for d in descriptions if not d["source"] and not d["sub_source"]),
        None,
    )
    return {
        "sku":                 sku,
        "stock_item_id":       stock_item_id,
        "title":               item.get("ItemTitle"),
        "description_count":   len(descriptions),
        "default_description": default,
        "descriptions":        descriptions,
    }


@_tool()
def get_inventory_item_titles(sku: str) -> dict:
    """
    Read the channel-specific titles for ONE inventory item.

    Linnworks stores per-channel listing titles that can diverge from the base
    `ItemTitle`. They are keyed by (StockItemId, Source, SubSource), so an item
    can have a different title on each channel/store plus a default (Source and
    SubSource both blank). The base `ItemTitle` (set via
    `create_or_update_inventory_item`) is separate and returned here as
    `base_title` for comparison. This is the read-side companion to
    `set_inventory_item_titles`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, base_title
          - title_count: number of channel-title rows
          - default_title: the text of the default row (blank Source +
            SubSource), or None if no default row exists
          - titles: per-row entries (source, sub_source, title, pk_row_id)
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemTitles",
        params={"inventoryItemId": stock_item_id},
    )
    titles = [
        {
            "source":     r.get("Source", ""),
            "sub_source": r.get("SubSource", ""),
            "title":      r.get("Title"),
            "pk_row_id":  r.get("pkRowId"),
        }
        for r in (rows if isinstance(rows, list) else [])
    ]
    default = next(
        (t["title"] for t in titles if not t["source"] and not t["sub_source"]),
        None,
    )
    return {
        "sku":           sku,
        "stock_item_id": stock_item_id,
        "base_title":    item.get("ItemTitle"),
        "title_count":   len(titles),
        "default_title": default,
        "titles":        titles,
    }


@_tool()
def set_inventory_item_titles(
    titles: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update channel-specific titles on Linnworks inventory items.

    Linnworks stores per-channel listing titles that can diverge from the base
    `ItemTitle`. They are keyed by (StockItemId, Source, SubSource), so you can
    set a different title per channel. Pass source="" and sub_source="" to set the
    default channel-title row.

    Note: this writes the channel-title overrides only — it does NOT change the
    base `ItemTitle` (use `create_or_update_inventory_item` for that). When
    correcting product data, update both so listings don't go stale. Read current
    values first with `get_inventory_item_titles`.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        titles: List of dicts, each with:
            - sku (str):        Item SKU  [required]
            - title (str):      The title text  [required]
            - source (str):     Channel source (e.g. "AMAZON"). Defaults to "".
            - sub_source (str): Channel sub-source. Defaults to "".
        confirmed_count: For batches > 50, pass len(titles) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of title rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    for t in titles:
        _check_injection("title", t.get("title", ""))

    for i, t in enumerate(titles):
        if not t.get("sku"):
            raise ValueError(f"titles[{i}] is missing 'sku'.")
        if t.get("title") is None:
            raise ValueError(f"titles[{i}] (SKU '{t.get('sku')}') is missing 'title'.")

    sku_cache: dict[str, str] = {}
    existing_titles: dict[str, list] = {}

    manifest = []
    for t in titles:
        sku        = t["sku"].strip()
        source     = t.get("source", "")
        sub_source = t.get("sub_source", "")
        new_title  = t["title"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "title": new_title, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_titles:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemTitles",
                params={"inventoryItemId": stock_item_id},
            )
            existing_titles[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_titles[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "source":        source,
            "sub_source":    sub_source,
            "old_title":     match.get("Title") if match else None,
            "new_title":     new_title,
            "action":        "update" if match else "create",
            "pk_row_id":     match.get("pkRowId") if match else None,
        })

    guard = _write_guard("set_inventory_item_titles", titles, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(titles),
            "manifest":   manifest,
            "message":    "Dry run — no titles changed. Set dry_run=False to execute.",
        }

    to_create  = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update  = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Title":       m["new_title"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemTitles",
            {"inventoryItemTitles": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Title":       m["new_title"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemTitles",
            {"inventoryItemTitles": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_title": m["old_title"],
                "new_title": m["new_title"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(titles),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def get_inventory_item_suppliers(sku: str) -> dict:
    """
    Read the purchasable supplier links for ONE inventory item — answers
    "which supplier(s) can this item be bought from, at what code and cost,
    and which is the primary/default?".

    Reads the StockItemSuppliers table (Inventory/GetStockSupplierStat). This
    is the per-item link table that purchase ordering and supplier reporting
    key off — `get_sales_by_supplier` attributes sales via the IsDefault row
    here. It is distinct from `get_suppliers` (the global supplier list, no
    per-item association) and from the flat `purchase_price` on the item.

    An item with zero rows is invisible to supplier-keyed exports and restock
    tooling — use `set_inventory_item_suppliers` to attach one.

    Args:
        sku: Exact SKU / ItemNumber of the item.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - supplier_count:   number of supplier links
          - default_supplier: name of the IsDefault row (None if none is default)
          - suppliers: rows with supplier, supplier_id, code, barcode,
            purchase_price, lead_time, min_order_qty, pack_size, currency,
            is_default
    """
    stock_item_id = _resolve_sku_to_id(sku)
    item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    rows = call_linnworks_get(
        "Inventory/GetStockSupplierStat", params={"inventoryItemId": stock_item_id}
    )
    rows = rows if isinstance(rows, list) else []
    suppliers = [
        {
            "supplier":       r.get("Supplier"),
            "supplier_id":    r.get("SupplierID"),
            "code":           r.get("Code"),
            "barcode":        r.get("SupplierBarcode"),
            "purchase_price": r.get("PurchasePrice"),
            "lead_time":      r.get("LeadTime"),
            "min_order_qty":  r.get("SupplierMinOrderQty"),
            "pack_size":      r.get("SupplierPackSize"),
            "currency":       r.get("SupplierCurrency"),
            "is_default":     bool(r.get("IsDefault")),
        }
        for r in rows
    ]
    default_row = next((s for s in suppliers if s["is_default"]), None)
    return {
        "sku":              sku,
        "stock_item_id":    stock_item_id,
        "title":            item.get("ItemTitle"),
        "supplier_count":   len(suppliers),
        "default_supplier": default_row["supplier"] if default_row else None,
        "suppliers":        suppliers,
    }


_SUPPLIER_LINK_FIELDS = {
    "supplier_code": "Code",
    "cost":          "PurchasePrice",
    "is_default":    "IsDefault",
    "lead_time":     "LeadTime",
    "min_order_qty": "SupplierMinOrderQty",
    "pack_size":     "SupplierPackSize",
    "barcode":       "SupplierBarcode",
    "currency":      "SupplierCurrency",
}


@_tool()
def set_inventory_item_suppliers(
    links: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add or update purchasable supplier links on inventory items — "this item
    can be bought from supplier X, under code Y, at cost Z".

    Upserts the StockItemSuppliers table keyed by (item, supplier): an existing
    link for that supplier is UPDATED (only the fields you pass change — the
    tool reads the current row first and carries every other field through,
    because Linnworks CLEARS any field omitted from an update); a new supplier
    gets a CREATED link. Existing links to other suppliers are never touched,
    except that Linnworks itself auto-flips the previous default row to
    IsDefault=false when a new default is written (live-confirmed).

    The supplier is matched by name (case-insensitive) against the configured
    supplier list (`get_suppliers`) — a supplier GUID is also accepted. An
    unknown supplier becomes a per-item error row; it never sinks the batch.

    Why this matters: purchase ordering and supplier reporting key off this
    table — `get_sales_by_supplier` attributes sales via the IsDefault link, so
    an item with no default supplier link is invisible to supplier-keyed
    exports and restock tooling. Give an item's primary supplier
    is_default=True.

    For batches larger than 50 links this tool requires confirmed_count=<N>.

    Args:
        links: List of dicts, each with:
            - sku (str):            Item SKU  [required]
            - supplier (str):       Supplier name (case-insensitive) or GUID  [required]
            - supplier_code (str):  The supplier's own SKU/code for this item
            - cost (float):         Purchase price from this supplier
            - is_default (bool):    Make this the primary supplier (see above)
            - lead_time (int):      Lead time in days
            - min_order_qty (int):  Minimum order quantity
            - pack_size (int):      Supplier pack size
            - barcode (str):        Supplier barcode
            - currency (str):       Supplier currency (e.g. "GBP")
          Only sku and supplier are required; on update, omitted fields keep
          their current values.
        confirmed_count: For batches > 50, pass len(links) here.
        dry_run: If True (default), returns the manifest without writing.

    Returns:
        A dict with:
          - dry_run, item_count
          - manifest: per-link preview (action create/update, old vs new values)
          - results:  per-link outcome incl. read-back verification (live only)
          - created / updated / errors counts (live only)
    """
    for i, l in enumerate(links):
        if not l.get("sku"):
            raise ValueError(f"links[{i}] is missing 'sku'.")
        if not l.get("supplier"):
            raise ValueError(f"links[{i}] (SKU '{l.get('sku')}') is missing 'supplier'.")
        for field in ("supplier", "supplier_code", "barcode", "currency"):
            _check_injection(field, str(l.get(field) or ""))

    supplier_rows = get_suppliers().get("suppliers", [])
    by_name = {(s.get("name") or "").strip().lower(): s for s in supplier_rows}
    by_id   = {(s.get("supplier_id") or "").lower(): s for s in supplier_rows}

    sku_cache: dict[str, str] = {}
    existing_stats: dict[str, list] = {}

    manifest = []
    for l in links:
        sku = l["sku"].strip()
        sup_input = str(l["supplier"]).strip()
        sup = by_name.get(sup_input.lower()) or by_id.get(sup_input.lower())
        if sup is None:
            manifest.append({
                "sku": sku, "supplier": sup_input,
                "error": (
                    f"supplier '{sup_input}' not found in the configured supplier "
                    f"list — see get_suppliers. Known: "
                    f"{sorted(s.get('name') or '' for s in supplier_rows)}"
                ),
            })
            continue

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({"sku": sku, "supplier": sup["name"], "error": str(exc)})
            continue

        if stock_item_id not in existing_stats:
            rows = call_linnworks_get(
                "Inventory/GetStockSupplierStat",
                params={"inventoryItemId": stock_item_id},
            )
            existing_stats[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_stats[stock_item_id]
             if (r.get("SupplierID") or "").lower() == (sup["supplier_id"] or "").lower()),
            None,
        )

        changes = {
            api_field: l[user_field]
            for user_field, api_field in _SUPPLIER_LINK_FIELDS.items()
            if user_field in l and l[user_field] is not None
        }
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "supplier":      sup["name"],
            "supplier_id":   sup["supplier_id"],
            "action":        "update" if match else "create",
            "old":           {
                "code":           match.get("Code"),
                "purchase_price": match.get("PurchasePrice"),
                "is_default":     match.get("IsDefault"),
            } if match else None,
            "changes":       changes,
            "_existing_row": match,
        })

    public_manifest = [{k: v for k, v in m.items() if k != "_existing_row"} for m in manifest]
    guard = _write_guard("set_inventory_item_suppliers", links, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": public_manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(links),
            "manifest":   public_manifest,
            "message":    "Dry run — no supplier links changed. Set dry_run=False to execute.",
        }

    results = []
    created = updated = errors = 0
    touched_ids: dict[str, str] = {}

    for m in manifest:
        if m.get("error"):
            errors += 1
            results.append({"sku": m["sku"], "supplier": m.get("supplier"),
                            "action": "error", "error": m["error"]})
            continue

        if m["action"] == "update":
            row = dict(m["_existing_row"])
            row.pop("StockItemIntId", None)
            row.update(m["changes"])
            row["StockItemId"] = m["stock_item_id"]
            row["SupplierID"]  = m["supplier_id"]
            endpoint = "Inventory/UpdateStockSupplierStat"
        else:
            row = {
                "StockItemId": m["stock_item_id"],
                "SupplierID":  m["supplier_id"],
                "Supplier":    m["supplier"],
                **m["changes"],
            }
            endpoint = "Inventory/CreateStockSupplierStat"

        try:
            call_linnworks_void(endpoint, {"itemSuppliers": [row]})
        except RuntimeError as exc:
            errors += 1
            results.append({"sku": m["sku"], "supplier": m["supplier"],
                            "action": "error", "error": str(exc)})
            continue

        touched_ids[m["stock_item_id"]] = m["sku"]
        if m["action"] == "update":
            updated += 1
        else:
            created += 1
        results.append({
            "sku": m["sku"], "supplier": m["supplier"], "action": f"{m['action']}d",
        })

    read_back = {}
    for sid, sku in touched_ids.items():
        rows = call_linnworks_get(
            "Inventory/GetStockSupplierStat", params={"inventoryItemId": sid}
        )
        read_back[sku] = [
            {"supplier": r.get("Supplier"), "code": r.get("Code"),
             "purchase_price": r.get("PurchasePrice"), "is_default": r.get("IsDefault")}
            for r in (rows if isinstance(rows, list) else [])
        ]

    return {
        "dry_run":    False,
        "item_count": len(links),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "read_back":  read_back,
        "manifest":   public_manifest,
    }


@_tool()
def add_inventory_item_images(
    images: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add images to Linnworks inventory items by URL.

    Each entry attaches one image URL to a stock item.  Images are additive —
    existing images are not removed.  Use is_main=True to set the uploaded
    image as the item's primary image.

    For batches larger than 100 items this tool requires confirmed_count=<N>.

    Args:
        images: List of dicts, each with:
            - sku (str):        Item SKU  [required]
            - image_url (str):  Publicly accessible URL of the image  [required]
            - is_main (bool):   Set as the main product image. Defaults to False.
        confirmed_count: For batches > 100, pass len(images) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of image rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - added:       images successfully added (live run only)
          - errors:      images that failed (live run only)
    """
    for img in images:
        _check_injection("image_url", img.get("image_url", ""))

    for i, img in enumerate(images):
        if not img.get("sku"):
            raise ValueError(f"images[{i}] is missing 'sku'.")
        if not img.get("image_url"):
            raise ValueError(f"images[{i}] (SKU '{img.get('sku')}') is missing 'image_url'.")

    sku_cache: dict[str, str] = {}

    manifest = []
    for img in images:
        sku = img["sku"].strip()
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
            manifest.append({
                "sku":           sku,
                "stock_item_id": stock_item_id,
                "image_url":     img["image_url"],
                "is_main":       img.get("is_main", False),
            })
        except ValueError as exc:
            manifest.append({
                "sku": sku, "image_url": img.get("image_url"),
                "is_main": img.get("is_main", False), "error": str(exc),
            })

    guard = _write_guard("add_inventory_item_images", images, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(images),
            "manifest":   manifest,
            "message":    "Dry run — no images added. Set dry_run=False to execute.",
        }

    results = []
    added  = 0
    errors = 0

    for m in manifest:
        if m.get("error"):
            errors += 1
            results.append({"sku": m["sku"], "action": "error", "error": m["error"]})
            continue
        try:
            call_linnworks(
                "Inventory/AddImageToInventoryItem",
                {
                    "request": {
                        "StockItemId": m["stock_item_id"],
                        "ImageUrl":    m["image_url"],
                        "IsMain":      m["is_main"],
                    }
                },
            )
            added += 1
            results.append({
                "sku": m["sku"], "action": "added",
                "image_url": m["image_url"], "is_main": m["is_main"],
            })
        except Exception as exc:
            errors += 1
            results.append({"sku": m["sku"], "action": "error", "error": str(exc)})

    return {
        "dry_run":    False,
        "item_count": len(images),
        "added":      added,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@_tool()
def create_variation_group(
    group_name: str,
    parent_sku: str,
    child_skus: list[str],
    dry_run: bool = True,
) -> dict:
    """
    Create a variation group in Linnworks, linking a NEW variation parent to
    its existing child items.

    A variation group connects items that are variants of the same product
    (e.g. a T-shirt in different sizes/colours). The parent is the variation
    template that carries the group; the child SKUs are the individual sellable
    variants.

    ⚠️ The `parent_sku` is a NEW variation SKU that Linnworks MINTS as it
    creates the group — it must NOT already exist as a normal stock item.
    (Live-confirmed 28 Jul 2026: passing an existing item's SKU returns HTTP 400
    "Chosen variation SKU already exists as a normal stock item.") The child
    SKUs, by contrast, MUST already exist. To add more children to a group that
    already exists, use `add_variation_group_items` instead.

    Args:
        group_name: The name / title for the variation group.  [required]
        parent_sku: The NEW variation-parent SKU to create (must not already
            exist as a stock item or variation).  [required]
        child_skus: List of EXISTING SKUs that are children (variants) of the
            parent. The parent SKU is not included here.  [required]
        dry_run: If True (default), validates the parent is free + children
            exist and shows what would be created without writing. Set to False
            to execute.

    Returns:
        A dict with:
          - dry_run:          whether this was a dry run
          - group_name:       the requested group name
          - parent_sku:       the new parent variation SKU
          - child_skus:       the child SKUs
          - pk_variation_item_id: the minted group id (live run)
          - child_ids:        resolved child StockItemIds
          - status:           "dry_run", "created", "already_exists", or "error"
          - message:          human-readable outcome
    """
    _check_injection("group_name", group_name)

    try:
        parent_state = call_linnworks_get(
            "Stock/CheckVariationParentSKUExists", {"parentSKU": parent_sku}
        )
    except RuntimeError:
        parent_state = None
    if parent_state and str(parent_state).strip() != "NotExists":
        hint = (
            "it is already a variation parent — use add_variation_group_items "
            "to add children to it"
            if str(parent_state).strip() == "AlreadyVariation"
            else "it already exists as a normal stock item; the variation "
                 "parent SKU must be a brand-new SKU"
        )
        return {
            "dry_run": dry_run, "group_name": group_name,
            "parent_sku": parent_sku, "child_skus": child_skus,
            "status": "error",
            "message": f"Cannot use '{parent_sku}' as the variation parent — {hint}.",
            "parent_sku_state": str(parent_state).strip(),
        }

    sku_cache: dict[str, str] = {}
    child_ids = []
    child_errors = []
    for sku in child_skus:
        try:
            child_ids.append(_resolve_sku_to_id(sku, sku_cache))
        except ValueError as exc:
            child_errors.append({"sku": sku, "error": str(exc)})

    if child_errors:
        return {
            "dry_run": dry_run, "group_name": group_name,
            "parent_sku": parent_sku, "child_skus": child_skus,
            "status": "error",
            "message": f"{len(child_errors)} child SKU(s) could not be resolved.",
            "child_errors": child_errors,
        }

    existing_group = _find_variation_group_by_name(group_name)
    if existing_group:
        return {
            "dry_run":    dry_run,
            "group_name": group_name,
            "parent_sku": parent_sku,
            "child_skus": child_skus,
            "status":     "already_exists",
            "message": (
                f"A variation group named '{group_name}' already exists "
                f"in Linnworks (parent '{existing_group.get('VariationSKU')}'). "
                f"No new group was created — use add_variation_group_items to "
                f"add children to it."
            ),
            "existing_group": existing_group,
        }

    base = {
        "dry_run":    dry_run,
        "group_name": group_name,
        "parent_sku": parent_sku,
        "child_skus": child_skus,
        "child_ids":  child_ids,
    }

    if dry_run:
        return {
            **base,
            "status":  "dry_run",
            "message": (
                f"Dry run — would create variation group '{group_name}' with a "
                f"new parent '{parent_sku}' and {len(child_skus)} existing "
                f"child(ren). Set dry_run=False to create."
            ),
        }

    created = call_linnworks(
        "Stock/CreateVariationGroup",
        {
            "template": {
                "VariationGroupName": group_name,
                "ParentSKU":          parent_sku,
                "ParentStockItemId":  "00000000-0000-0000-0000-000000000000",
                "VariationItemIds":   child_ids,
            }
        },
    )
    pk = created.get("pkVariationItemId") if isinstance(created, dict) else None

    members = []
    if pk:
        members = call_linnworks_get(
            "Stock/GetVariationItems", {"pkVariationItemId": pk}
        ) or []

    return {
        **base,
        "status":              "created",
        "pk_variation_item_id": pk,
        "confirmed_group":     created,
        "member_count":        len(members),
        "message": (
            f"Variation group '{group_name}' created with new parent "
            f"'{parent_sku}' and {len(child_ids)} child item(s)."
        ),
    }


@_tool()
def add_variation_group_items(
    child_skus: list[str],
    parent_sku: str | None = None,
    group_name: str | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add one or more child SKUs to an EXISTING variation group.

    The counterpart to `create_variation_group`, which is create-only: it
    no-ops the moment a group with that name exists and has no code path that
    would ever attach a new child. Use THIS tool to link an extra size/colour
    into a group that already exists.

    Why this is needed: supplier feeds add new variants over time (e.g. a
    product launches S/M/L, the supplier adds XS weeks later). The CSV import
    creates the new child item but does not reliably re-link it into an
    already-live group — it stays orphaned (an item, not a member) until
    someone opens the product in the Linnworks UI and revises the group. This
    tool is the API path to do that link. Detect the gap first with
    `get_item_relationships` (compare the group's live children against the
    expected set), then close it here.

    Identify the group by EITHER `parent_sku` OR `group_name` (at least one is
    required). If both are given they must resolve to the same group or the
    call errors. The group id used by Linnworks is the PARENT item's
    StockItemId (== the group's `pkVariationItemId`, confirmed).

    Idempotent: only child SKUs not already in the group are added. SKUs
    already linked are reported under `already_present` and skipped, so it is
    safe to re-run with the full expected child set. A child SKU that cannot be
    resolved becomes a row in `child_errors` and does NOT abort the others.

    Args:
        child_skus: SKUs to add as children of the group. The parent SKU should
            not be included. [required]
        parent_sku: SKU of the variation parent identifying the group.
        group_name: Name of the variation group identifying it (alternative or
            complement to parent_sku).
        dry_run: If True (default), resolves SKUs, diffs against the current
            members and shows what would be added without writing. Set to False
            to execute.

    Returns:
        A dict with:
          - dry_run:               whether this was a dry run
          - status:                "dry_run", "added", "no_op", or "error"
          - group_name:            the resolved group name
          - parent_sku:            the resolved parent SKU
          - pk_variation_item_id:  the group id (parent StockItemId)
          - current_members:       SKUs already in the group before the write
          - to_add:                child SKUs that would be / were added
          - already_present:       requested SKUs already in the group (skipped)
          - child_errors:          [{sku, error}] for SKUs that failed to resolve
          - added:                 (live run) read-back per SKU {sku, added: bool}
          - message:               human-readable outcome
    """
    if group_name:
        _check_injection("group_name", group_name)

    if not parent_sku and not group_name:
        return {
            "dry_run": dry_run, "status": "error",
            "message": "Provide at least one of parent_sku or group_name to "
                       "identify the existing variation group.",
        }

    sku_cache: dict[str, str] = {}

    pk_variation_item_id: str | None = None
    resolved_parent_sku: str | None = None
    resolved_group_name: str | None = None

    if parent_sku:
        try:
            parent_id = _resolve_sku_to_id(parent_sku, sku_cache)
        except ValueError as exc:
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": f"Parent SKU resolution failed: {exc}",
            }
        group = call_linnworks_get(
            "Stock/GetVariationGroupByParentId", {"pkStockItemId": parent_id}
        )
        if not group or not group.get("pkVariationItemId"):
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": (
                    f"'{parent_sku}' is not a variation parent (no existing "
                    f"group hangs off it). Use create_variation_group to make a "
                    f"new group, or pass the correct parent SKU."
                ),
            }
        pk_variation_item_id = group.get("pkVariationItemId")
        resolved_parent_sku = parent_sku
        resolved_group_name = group.get("VariationGroupName")

        if group_name and (resolved_group_name or "").strip().lower() != group_name.strip().lower():
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": (
                    f"parent_sku '{parent_sku}' belongs to group "
                    f"'{resolved_group_name}', which does not match the "
                    f"group_name '{group_name}' you supplied."
                ),
            }
    else:
        existing = _find_variation_group_by_name(group_name)
        if not existing or not existing.get("pkVariationItemId"):
            return {
                "dry_run": dry_run, "status": "error",
                "group_name": group_name,
                "message": (
                    f"No variation group named '{group_name}' exists in "
                    f"Linnworks. Use create_variation_group to make it."
                ),
            }
        pk_variation_item_id = existing.get("pkVariationItemId")
        resolved_parent_sku = existing.get("VariationSKU")
        resolved_group_name = existing.get("VariationGroupName")

    members = call_linnworks_get(
        "Stock/GetVariationItems", {"pkVariationItemId": pk_variation_item_id}
    ) or []
    current_member_skus = [
        (m.get("ItemNumber") or m.get("SKU") or "") for m in members
    ]
    current_lower = {s.strip().lower() for s in current_member_skus if s}

    to_add: list[str] = []
    to_add_ids: list[str] = []
    already_present: list[str] = []
    child_errors: list[dict] = []
    seen_in_request: set[str] = set()

    for sku in child_skus:
        key = (sku or "").strip().lower()
        if not key or key in seen_in_request:
            continue
        seen_in_request.add(key)
        if key in current_lower:
            already_present.append(sku)
            continue
        try:
            to_add_ids.append(_resolve_sku_to_id(sku, sku_cache))
            to_add.append(sku)
        except ValueError as exc:
            child_errors.append({"sku": sku, "error": str(exc)})

    base = {
        "dry_run":              dry_run,
        "group_name":           resolved_group_name,
        "parent_sku":           resolved_parent_sku,
        "pk_variation_item_id": pk_variation_item_id,
        "current_members":      current_member_skus,
        "to_add":               to_add,
        "already_present":      already_present,
        "child_errors":         child_errors,
    }

    if not to_add:
        msg_bits = []
        if already_present:
            msg_bits.append(f"{len(already_present)} already in the group")
        if child_errors:
            msg_bits.append(f"{len(child_errors)} could not be resolved")
        detail = "; ".join(msg_bits) if msg_bits else "no valid children supplied"
        return {
            **base,
            "status": "no_op" if not child_errors else "error",
            "message": (
                f"No children to add to '{resolved_group_name}' ({detail})."
            ),
        }

    if dry_run:
        return {
            **base,
            "status": "dry_run",
            "message": (
                f"Dry run — would add {len(to_add)} child(ren) to variation "
                f"group '{resolved_group_name}' "
                f"({len(already_present)} already present, "
                f"{len(child_errors)} unresolved). Set dry_run=False to add."
            ),
        }

    call_linnworks(
        "Stock/AddVariationItems",
        {
            "pkVariationItemId": pk_variation_item_id,
            "pkStockItemIds":    to_add_ids,
        },
    )

    after = call_linnworks_get(
        "Stock/GetVariationItems", {"pkVariationItemId": pk_variation_item_id}
    ) or []
    after_lower = {
        (m.get("ItemNumber") or m.get("SKU") or "").strip().lower()
        for m in after
    }
    added = [
        {"sku": sku, "added": (sku or "").strip().lower() in after_lower}
        for sku in to_add
    ]
    added_ok = sum(1 for a in added if a["added"])

    return {
        **base,
        "status":          "added",
        "added":           added,
        "member_count_after": len(after),
        "message": (
            f"Added {added_ok}/{len(to_add)} child(ren) to variation group "
            f"'{resolved_group_name}'. "
            f"{len(already_present)} were already present"
            + (f", {len(child_errors)} could not be resolved" if child_errors else "")
            + "."
        ),
    }


@_tool()
def delete_inventory_item(
    skus: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete one or more inventory items from Linnworks by SKU.

    ⚠️  IRREVERSIBLE.  Deleted items CANNOT be restored via the API — their
    stock levels, prices, descriptions, extended properties, images, and
    listing links are all destroyed.  The dry-run manifest and the staging
    gate are the main safety nets.  Always run dry_run=True first and read
    the manifest carefully before setting dry_run=False.

    Deleting a composite parent, a variation parent, or an item that is on an
    active channel listing may have side effects — any Linnworks error is
    surfaced verbatim per item.

    For batches larger than 10 items this tool enters a staging mode: it
    returns a manifest of exactly what would be destroyed and asks you to
    confirm with confirmed_count=<N> before executing.

    Uses Inventory/DeleteInventoryItems (payload sent UNWRAPPED — empty 2xx
    body on success; confirmed live 15 Jun 2026).

    Args:
        skus: List of exact SKUs / ItemNumbers to delete.  Each is resolved to
            its StockItemId before deletion.  A SKU that does not resolve is
            reported as an error row and does NOT abort the rest of the batch.
        confirmed_count: For batches > 10, pass len(skus) here after reviewing
            the manifest to confirm the destruction.
        dry_run: If True (default), returns the manifest of what would be
            deleted without writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of SKUs in the batch
          - manifest:    per-item preview (sku, stock_item_id, title,
                         current_stock, resolved/error) — always present
          - results:     per-item outcome with deleted flag (live run only)
          - deleted:     count of items confirmed gone (live run only)
          - errors:      count of SKUs that failed to resolve or delete
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    sku_cache: dict[str, str] = {}
    manifest: list[dict] = []

    for sku in skus:
        sku = (sku or "").strip()
        if not sku:
            manifest.append({
                "sku": sku, "stock_item_id": None, "title": None,
                "current_stock": None, "resolved": False,
                "error": "empty SKU",
            })
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            manifest.append({
                "sku": sku, "stock_item_id": None, "title": None,
                "current_stock": None, "resolved": False,
                "error": f"not found: {exc}",
            })
            continue

        stock_item_id = item.get("StockItemId")
        if not stock_item_id:
            manifest.append({
                "sku": sku, "stock_item_id": None,
                "title": item.get("ItemTitle"),
                "current_stock": None, "resolved": False,
                "error": "found but returned no StockItemId",
            })
            continue

        sku_cache[sku] = stock_item_id

        current_stock: int | None = None
        try:
            levels_resp = call_linnworks(
                "Stock/GetStockLevel_Batch",
                {"request": {"StockItemIds": [stock_item_id]}},
            )
            rows = levels_resp if isinstance(levels_resp, list) else (
                levels_resp.get("StockItemLevels") or []
            )
            total = 0
            for row_group in rows:
                for row in (row_group if isinstance(row_group, list) else [row_group]):
                    lvl = row.get("StockLevel")
                    if isinstance(lvl, (int, float)):
                        total += lvl
            current_stock = total
        except (ValueError, RuntimeError):
            current_stock = None

        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "title":         item.get("ItemTitle"),
            "current_stock": current_stock,
            "resolved":      True,
        })

    resolved_rows = [m for m in manifest if m.get("resolved")]
    resolve_errors = [m for m in manifest if not m.get("resolved")]

    guard = _write_guard("delete_inventory_item", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(skus),
            "manifest":   manifest,
            "message": (
                f"Dry run — nothing deleted. {len(resolved_rows)} item(s) would be "
                f"PERMANENTLY deleted, {len(resolve_errors)} could not be resolved. "
                "Review the manifest, then set dry_run=False to execute. "
                "This cannot be undone."
            ),
        }

    if not resolved_rows:
        return {
            "dry_run":    False,
            "item_count": len(skus),
            "deleted":    0,
            "errors":     len(resolve_errors),
            "results":    [],
            "manifest":   manifest,
            "message":    "No SKUs resolved to a StockItemId; nothing was deleted.",
        }

    ids_to_delete = [m["stock_item_id"] for m in resolved_rows]
    delete_error: str | None = None
    try:
        call_linnworks(
            "Inventory/DeleteInventoryItems",
            {"inventoryItemIds": ids_to_delete},
        )
    except RuntimeError as exc:
        delete_error = str(exc)

    results = []
    deleted = 0
    errors = len(resolve_errors)
    for m in resolved_rows:
        sku = m["sku"]
        gone = False
        probe_note = None
        try:
            call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
            gone = False
        except RuntimeError:
            gone = True
        if gone:
            deleted += 1
        else:
            errors += 1
            probe_note = "still exists after delete call"
        results.append({
            "sku":           sku,
            "stock_item_id": m["stock_item_id"],
            "title":         m.get("title"),
            "deleted":       gone,
            **({"note": probe_note} if probe_note else {}),
        })

    for m in resolve_errors:
        results.append({
            "sku":           m["sku"],
            "stock_item_id": None,
            "deleted":       False,
            "error":         m.get("error"),
        })

    out = {
        "dry_run":    False,
        "item_count": len(skus),
        "deleted":    deleted,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }
    if delete_error is not None:
        out["delete_error"] = delete_error
        out["message"] = (
            "Linnworks returned an error on DeleteInventoryItems (surfaced in "
            "delete_error). Per-item read-back results show what actually got deleted."
        )
    return out


_SKU_NOT_FOUND_MARKER = "could not determine inventory item id from sku"

_RENAME_SKU_CHANNEL_WARNING = (
    "Channel listings are NOT renamed. Shopify, Amazon, eBay and other channels still "
    "carry the OLD SKU, and so do this item's Linnworks channel-SKU rows "
    "(get_channel_listings). Linked listings may need relinking, and orders that link "
    "by SKU may arrive unlinked — check find_unlinked_order_lines and fix with "
    "relink_order_line."
)


def _sku_lookup(sku: str) -> tuple[str, dict | None, str | None]:
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError as exc:
        return "rate_limited", None, str(exc)
    except RuntimeError as exc:
        if _SKU_NOT_FOUND_MARKER in str(exc).lower():
            return "not_found", None, str(exc)
        return "failed", None, str(exc)
    if not (item or {}).get("StockItemId"):
        return "failed", None, f"GetInventoryItem returned no StockItemId for '{sku}'."
    return "found", item, None


@_tool()
def rename_inventory_item_sku(
    renames: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Rename an inventory item's SKU, keeping it the SAME item.

    Use this instead of create_or_update_inventory_item when a SKU needs to
    change: that tool upserts by SKU, so a new SKU there creates a duplicate
    item. This one changes the SKU on the existing record. The StockItemId is
    kept, so purchase order lines, supplier links, the barcode, extended
    properties, descriptions, images and stock levels all follow the rename.

    ⚠️  Channel listings are NOT renamed. Shopify, Amazon, eBay and every other
    channel still carry the OLD SKU, and so do the item's Linnworks
    channel-SKU rows (see get_channel_listings). Linked listings may need
    relinking or re-mapping in the channel's own admin, and new orders that
    link by SKU may arrive unlinked — check find_unlinked_order_lines and fix
    them with relink_order_line. Anything else that refers to the item by its
    old SKU (supplier stock feeds, imports, spreadsheets) stops matching too.

    Each row is checked before anything is written, and a row that fails a
    check is refused on its own — the rest of the batch still goes ahead:
      - old_sku must resolve to an active item (archived items cannot be
        resolved by SKU, so they cannot be renamed here)
      - new_sku must not already belong to another item, INCLUDING an archived
        one (checked with a second lookup that can see archived items)
      - a lookup that fails or is rate-limited is refused, never treated as
        "the new SKU is free"
      - two rows may not rename to the same new SKU, or rename the same old SKU
        (Linnworks matches SKUs case-insensitively, so "ABC" and "abc" collide)
      - renaming onto a SKU that another row in the same batch is renaming away
        from is refused; run the two renames as separate calls instead
    A change of letter case only ("abc" → "ABC") is allowed.

    On a live run every item is read back by its StockItemId and the rename
    only counts when the stored SKU now equals new_sku exactly.

    Args:
        renames: List of {"old_sku": str, "new_sku": str}. Surrounding
            whitespace is stripped.
        confirmed_count: For more than 10 rows, pass len(renames) after
            reviewing the staged manifest.
        dry_run: If True (default), preview only. Set False to rename.

    Returns:
        dict with:
          - manifest: rows that will be (or were) renamed — old_sku, new_sku,
            stock_item_id, title, case_only
          - refused: rows not renamed, each with a `reason` (old_sku_not_found,
            lookup_failed, new_sku_exists, new_sku_check_failed, rate_limited,
            same_sku, duplicate_old_sku_in_batch, duplicate_new_sku_in_batch,
            invalid_row) and the Linnworks error where there was one
          - complete: False when any row was rate-limited (retry those later)
          - warnings: the channel-listing warning above
          - results (live run only): one row per rename with `outcome`
            (renamed / not_renamed / unconfirmed / error / rate_limited) and
            `read_back_sku`, the SKU Linnworks holds now
    """
    if not renames:
        raise ValueError("renames must contain at least one {old_sku, new_sku} row.")

    refused: list[dict] = []

    def _refuse(row: dict, reason: str, error: str, **extra) -> None:
        refused.append({"old_sku": row.get("old_sku"), "new_sku": row.get("new_sku"),
                        "reason": reason, "error": error, **extra})

    rows: list[dict] = []
    for raw in renames:
        if not isinstance(raw, dict):
            _refuse({}, "invalid_row",
                    f"Each row must be a dict with old_sku and new_sku, got {raw!r}.")
            continue
        old = raw.get("old_sku")
        new = raw.get("new_sku")
        old = old.strip() if isinstance(old, str) else ""
        new = new.strip() if isinstance(new, str) else ""
        if not old or not new:
            _refuse({"old_sku": old or None, "new_sku": new or None}, "invalid_row",
                    "Both old_sku and new_sku are required as non-empty strings.")
            continue
        _check_injection("new_sku", new)
        if new == old:
            _refuse({"old_sku": old, "new_sku": new}, "same_sku",
                    "new_sku is identical to old_sku — there is nothing to rename.")
            continue
        rows.append({"old_sku": old, "new_sku": new})

    old_seen: dict[str, int] = {}
    new_seen: dict[str, int] = {}
    for r in rows:
        old_seen[r["old_sku"].casefold()] = old_seen.get(r["old_sku"].casefold(), 0) + 1
        new_seen[r["new_sku"].casefold()] = new_seen.get(r["new_sku"].casefold(), 0) + 1
    candidates: list[dict] = []
    for r in rows:
        if old_seen[r["old_sku"].casefold()] > 1:
            _refuse(r, "duplicate_old_sku_in_batch",
                    f"'{r['old_sku']}' appears more than once as old_sku in this batch.")
        elif new_seen[r["new_sku"].casefold()] > 1:
            _refuse(r, "duplicate_new_sku_in_batch",
                    f"More than one row renames to '{r['new_sku']}' (case-insensitive) — "
                    "only one item can hold a SKU.")
        else:
            candidates.append(r)

    manifest: list[dict] = []
    for r in candidates:
        state, item, err = _sku_lookup(r["old_sku"])
        if state == "rate_limited":
            _refuse(r, "rate_limited", f"{err} Not a missing SKU — retry later.")
            continue
        if state == "not_found":
            _refuse(r, "old_sku_not_found",
                    f"{err} Archived items cannot be resolved by SKU, so they cannot be "
                    "renamed here.")
            continue
        if state != "found":
            _refuse(r, "lookup_failed", err)
            continue
        sid = item["StockItemId"]

        nstate, holder, nerr = _sku_lookup(r["new_sku"])
        if nstate == "rate_limited":
            _refuse(r, "rate_limited", f"{nerr} Not proof the new SKU is free — retry later.")
            continue
        if nstate == "failed":
            _refuse(r, "new_sku_check_failed",
                    f"Could not prove '{r['new_sku']}' is free: {nerr}")
            continue

        case_only = False
        if nstate == "found":
            if (holder["StockItemId"] or "").lower() != sid.lower():
                _refuse(r, "new_sku_exists",
                        f"'{r['new_sku']}' already belongs to another item "
                        f"({holder.get('ItemNumber')}, {holder['StockItemId']}).",
                        held_by_stock_item_id=holder["StockItemId"],
                        held_by_sku=holder.get("ItemNumber"),
                        held_by_title=holder.get("ItemTitle"))
                continue
            case_only = True
        else:
            try:
                answer = call_linnworks_get(
                    "Stock/CheckVariationParentSKUExists", {"parentSKU": r["new_sku"]})
            except RateLimitError as exc:
                _refuse(r, "rate_limited", f"{exc} Not proof the new SKU is free — retry later.")
                continue
            except RuntimeError as exc:
                _refuse(r, "new_sku_check_failed",
                        f"Could not prove '{r['new_sku']}' is free: {exc}")
                continue
            answer = str(answer).strip()
            if answer in ("Exists", "AlreadyVariation"):
                _refuse(r, "new_sku_exists",
                        f"'{r['new_sku']}' already exists (CheckVariationParentSKUExists: "
                        f"{answer}) on an item GetInventoryItem cannot see — most likely "
                        "an archived item.")
                continue
            if answer != "NotExists":
                _refuse(r, "new_sku_check_failed",
                        f"Unexpected CheckVariationParentSKUExists answer {answer!r} for "
                        f"'{r['new_sku']}' — cannot prove it is free.")
                continue

        manifest.append({
            "old_sku":       r["old_sku"],
            "new_sku":       r["new_sku"],
            "stock_item_id": sid,
            "title":         item.get("ItemTitle"),
            "case_only":     case_only,
        })

    base_out = {
        "item_count":    len(renames),
        "manifest":      manifest,
        "refused":       refused,
        "ready_count":   len(manifest),
        "refused_count": len(refused),
        "complete":      not any(x["reason"] == "rate_limited" for x in refused),
        "warnings":      [_RENAME_SKU_CHANNEL_WARNING],
    }

    guard = _write_guard("rename_inventory_item_sku", renames, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing renamed. {len(manifest)} item(s) would be renamed; "
                f"{len(refused)} row(s) refused (see refused[].reason). Channel listings "
                "keep the old SKU — see warnings. Set dry_run=False to rename."
            ),
        }

    results: list[dict] = []
    for m in manifest:
        row = {"old_sku": m["old_sku"], "new_sku": m["new_sku"],
               "stock_item_id": m["stock_item_id"]}
        write_error = None
        try:
            call_linnworks_void("Inventory/UpdateInventoryItemField", {
                "inventoryItemId": m["stock_item_id"],
                "fieldName":       "SKU",
                "fieldValue":      m["new_sku"],
            })
        except RateLimitError as exc:
            results.append({**row, "outcome": "rate_limited", "error": str(exc)})
            continue
        except RuntimeError as exc:
            write_error = str(exc)

        try:
            fresh = call_linnworks_get("Inventory/GetInventoryItemById",
                                       {"id": m["stock_item_id"]})
            readback = "ok" if fresh else "item_not_returned"
        except RateLimitError:
            fresh, readback = None, "rate_limited"
        except Exception as exc:
            fresh, readback = None, f"failed: {exc}"
        got = (fresh or {}).get("ItemNumber") if isinstance(fresh, dict) else None
        row["readback"] = readback
        row["read_back_sku"] = got

        if readback == "ok" and got == m["new_sku"]:
            row["outcome"] = "renamed"
            if write_error:
                row["warning"] = ("The write reported an error but the read-back shows the "
                                  f"new SKU: {write_error}")
        elif write_error:
            row["outcome"] = "error"
            row["error"] = write_error
        elif readback == "ok":
            row["outcome"] = "not_renamed"
        else:
            row["outcome"] = "unconfirmed"
        results.append(row)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1

    out = {
        "dry_run":            False,
        **base_out,
        "results":            results,
        "renamed_count":      counts.get("renamed", 0),
        "not_renamed_count":  counts.get("not_renamed", 0),
        "unconfirmed_count":  counts.get("unconfirmed", 0),
        "error_count":        counts.get("error", 0),
        "rate_limited_count": counts.get("rate_limited", 0),
    }
    if counts.get("rate_limited"):
        out["complete"] = False
    out["message"] = (
        f"{out['renamed_count']}/{len(results)} rename(s) confirmed by read-back; "
        f"{len(refused)} row(s) refused before writing. "
        + (f"{out['not_renamed_count']} not renamed (the write returned OK but the SKU "
           "did not change). " if out["not_renamed_count"] else "")
        + (f"{out['unconfirmed_count']} unconfirmed (read-back failed — check with "
           "find_inventory_item). " if out["unconfirmed_count"] else "")
        + "Channel listings still carry the old SKUs — see warnings."
    )
    return out


_DEFAULT_CATEGORY_ID = "00000000-0000-0000-0000-000000000000"


def _is_category_in_use(err: str) -> bool:
    return "category is in use" in (err or "").lower()


def _fetch_categories() -> list[dict]:
    cats = call_linnworks_get("Inventory/GetCategories")
    return cats if isinstance(cats, list) else []


def _format_category(c: dict, item_count: int | None = None) -> dict:
    out = {
        "category_id":         c.get("CategoryId"),
        "category_name":       c.get("CategoryName"),
        "product_category_id": c.get("ProductCategoryId"),
        "is_default":          (c.get("CategoryId") or "").lower() == _DEFAULT_CATEGORY_ID,
    }
    if item_count is not None:
        out["item_count"] = item_count
        out["is_empty"] = item_count == 0
    return out


def _count_items_per_category() -> dict:
    counts: dict[str, int] = {}
    total = 0
    pages = 0
    page = 1
    while True:
        resp = None
        for _ in range(6):
            try:
                resp = call_linnworks_get(
                    "Stock/GetStockItems",
                    {"keyWord": "", "entriesPerPage": 200, "pageNumber": page},
                )
                break
            except RuntimeError as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(15)
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                f"Category sweep aborted: repeated rate-limit (429) on page {page}. "
                "Emptiness could not be determined; no categories were changed."
            )
        data = resp.get("Data") or []
        pages += 1
        for it in data:
            cid = (it.get("CategoryId") or "").lower()
            counts[cid] = counts.get(cid, 0) + 1
            total += 1
        total_pages = resp.get("TotalPages") or 1
        if page >= total_pages or not data:
            break
        page += 1
        time.sleep(0.45)
    return {"counts": counts, "total_items": total, "pages": pages}


@_tool()
def get_categories(with_counts: bool = False) -> dict:
    """
    List all Linnworks inventory categories.

    By default this makes ONE fast API call and returns the category list. Pass
    with_counts=True to also report how many inventory items are in each
    category and flag the empty ones — but that triggers a FULL catalogue sweep
    (~2 minutes for a large catalogue) because Linnworks has no per-category
    count endpoint. Use with_counts=True when you want to find empty categories
    to clean up; leave it False for a quick name → id lookup.

    ⚠️  item_count / is_empty count ACTIVE items only — the sweep cannot see
    archived items (no Linnworks endpoint counts archived items per category).
    A category holding only archived stock shows item_count 0 / is_empty True
    here, yet Linnworks will refuse to delete it ("Category is in use"). Treat
    is_empty as "has no active items", not "safe to delete".

    Args:
        with_counts: If True, sweep the whole catalogue and add item_count +
            is_empty to every category (slow). Default False.

    Returns:
        - category_count: total number of categories
        - with_counts: echo of the flag
        - categories: list of {category_id, category_name, product_category_id,
            is_default[, item_count, is_empty]} sorted by name
        - (with_counts only) total_items, empty_count, used_count
    """
    cats = _fetch_categories()
    counts = None
    swept = None
    if with_counts:
        swept = _count_items_per_category()
        counts = swept["counts"]

    rows = []
    for c in cats:
        n = None if counts is None else counts.get((c.get("CategoryId") or "").lower(), 0)
        rows.append(_format_category(c, n))
    rows.sort(key=lambda r: (r.get("category_name") or "").lower())

    out = {
        "category_count": len(rows),
        "with_counts":    with_counts,
        "categories":     rows,
    }
    if with_counts:
        empty = [r for r in rows if r.get("is_empty")]
        out["total_items"] = swept["total_items"]
        out["empty_count"] = len(empty)
        out["used_count"]  = len(rows) - len(empty)
    return out


@_tool()
def create_category(name: str, dry_run: bool = True) -> dict:
    """
    Create a new inventory category.

    Single-category create. Checks for an existing category with the same name
    (case-insensitive) first and refuses to create a duplicate. dry_run=True by
    default — preview only; set dry_run=False to actually create.

    Uses Inventory/CreateCategory (Linnworks mints the CategoryId server-side).

    Args:
        name: The category name to create.
        dry_run: If True (default), preview only. Set False to create.

    Returns:
        A dict with created flag, category_id (live run only), and any
        duplicate note.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name must be a non-empty category name.")
    _check_injection("name", name)

    existing = _fetch_categories()
    dup = next(
        (c for c in existing
         if (c.get("CategoryName") or "").strip().lower() == name.lower()),
        None,
    )
    if dup:
        return {
            "created":              False,
            "dry_run":              dry_run,
            "category_name":        name,
            "existing_category_id": dup.get("CategoryId"),
            "message": (
                f"A category named '{dup.get('CategoryName')}' already exists "
                "— not creating a duplicate."
            ),
        }

    if dry_run:
        return {
            "created":       False,
            "dry_run":       True,
            "category_name": name,
            "message":       f"Dry run — would create category '{name}'. Set dry_run=False to create.",
        }

    resp = call_linnworks("Inventory/CreateCategory", {"categoryName": name})
    return {
        "created":       True,
        "dry_run":       False,
        "category_id":   resp.get("CategoryId"),
        "category_name": resp.get("CategoryName", name),
        "message":       f"Created category '{name}'.",
    }


@_tool()
def rename_category(category_id: str, new_name: str, dry_run: bool = True) -> dict:
    """
    Rename an existing inventory category.

    Reads the current category first (for a before/after diff), then updates its
    name while carrying every other field through unchanged — Inventory/Update
    Category clears any field you omit, so the full record must be re-sent.
    Refuses to rename the built-in Default category, and refuses a name already
    used by another category. dry_run=True by default; reads back after writing.

    Uses Inventory/UpdateCategory.

    Args:
        category_id: The CategoryId (GUID) to rename — from get_categories.
        new_name: The new category name.
        dry_run: If True (default), preview only. Set False to apply.
    """
    category_id = (category_id or "").strip()
    new_name = (new_name or "").strip()
    if not category_id:
        raise ValueError("category_id is required.")
    if not new_name:
        raise ValueError("new_name must be a non-empty category name.")
    _check_injection("new_name", new_name)
    if category_id.lower() == _DEFAULT_CATEGORY_ID:
        raise ValueError("The built-in Default category cannot be renamed.")

    cats = _fetch_categories()
    rec = next(
        (c for c in cats if (c.get("CategoryId") or "").lower() == category_id.lower()),
        None,
    )
    if rec is None:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"No category found with id {category_id}.",
        }

    old_name = rec.get("CategoryName")
    dup = next(
        (c for c in cats
         if (c.get("CategoryName") or "").strip().lower() == new_name.lower()
         and (c.get("CategoryId") or "").lower() != category_id.lower()),
        None,
    )
    if dup:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"Another category is already named '{dup.get('CategoryName')}'.",
        }

    if old_name == new_name:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"Category is already named '{new_name}' — nothing to do.",
        }

    if dry_run:
        return {
            "updated":     False,
            "dry_run":     True,
            "category_id": category_id,
            "before":      {"category_name": old_name},
            "after":       {"category_name": new_name},
            "message":     f"Dry run — would rename '{old_name}' → '{new_name}'. Set dry_run=False to apply.",
        }

    updated = dict(rec)
    updated["CategoryName"] = new_name
    call_linnworks("Inventory/UpdateCategory", {"category": updated})

    cats2 = _fetch_categories()
    rec2 = next(
        (c for c in cats2 if (c.get("CategoryId") or "").lower() == category_id.lower()),
        None,
    )
    now = rec2.get("CategoryName") if rec2 else None
    ok = now == new_name
    return {
        "updated":     ok,
        "dry_run":     False,
        "category_id": category_id,
        "before":      {"category_name": old_name},
        "after":       {"category_name": now},
        "message": (
            f"Renamed '{old_name}' → '{now}'." if ok
            else "Update sent, but read-back did not confirm the new name."
        ),
    }


@_tool()
def delete_categories(
    category_ids: list[str],
    force: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete specific inventory categories by CategoryId.

    ⚠️  Deleting a category that still has items reassigns those items to the
    Default category — it does NOT delete the items, but it does change their
    categorisation. By default this tool REFUSES to delete a non-empty category
    and reports it as blocked; pass force=True to delete non-empty categories
    anyway (their items move to Default). The built-in Default category can
    never be deleted.

    Emptiness is checked via a full catalogue sweep (~2 min) UNLESS force=True
    (which deletes regardless, so it skips the sweep). Use this tool when you
    have specific category ids to remove; to clean up ALL empty categories at
    once, use delete_empty_categories instead.

    ⚠️  The sweep counts ACTIVE items only. A category holding only ARCHIVED
    items looks empty (item_count 0) but Linnworks refuses to delete it
    ("Category is in use"). Such categories come back as `skipped_in_use` (not
    an error); the response reports `deleted` and `skipped_in_use` counts.
    force=True does NOT override this — it only skips the sweep; the server-side
    in-use guard still applies. Unarchive/reassign the items first to delete.

    Staging: batches over 10 categories require a confirmed_count echo-back
    after you review the manifest. dry_run=True by default.

    Uses Inventory/DeleteCategoryById (one call per category).

    Args:
        category_ids: List of CategoryId GUIDs to delete (from get_categories).
        force: If True, delete even non-empty categories (their items → Default)
            and skip the emptiness sweep. Default False (empty-only, safe).
        confirmed_count: For batches > 10, pass len(category_ids) after review.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        A dict with manifest (per-id: name, item_count, status of
        deletable/blocked/error) and, on a live run, per-id delete results.
    """
    if not category_ids:
        raise ValueError("category_ids must contain at least one CategoryId.")

    cats = _fetch_categories()
    by_id = {(c.get("CategoryId") or "").lower(): c for c in cats}

    counts = None
    if not force:
        counts = _count_items_per_category()["counts"]

    manifest: list[dict] = []
    for cid in category_ids:
        cid_norm = (cid or "").strip()
        key = cid_norm.lower()
        rec = by_id.get(key)
        if not cid_norm:
            manifest.append({"category_id": cid, "status": "error", "reason": "empty id"})
            continue
        if key == _DEFAULT_CATEGORY_ID:
            manifest.append({
                "category_id": cid_norm, "category_name": rec.get("CategoryName") if rec else "Default",
                "status": "blocked", "reason": "the built-in Default category cannot be deleted",
            })
            continue
        if rec is None:
            manifest.append({"category_id": cid_norm, "status": "error",
                             "reason": "no category found with this id"})
            continue

        row = {
            "category_id":   cid_norm,
            "category_name": rec.get("CategoryName"),
        }
        if counts is not None:
            n = counts.get(key, 0)
            row["item_count"] = n
            if n > 0:
                row["status"] = "blocked"
                row["reason"] = f"category has {n} item(s); pass force=True to delete (items → Default)"
            else:
                row["status"] = "deletable"
        else:
            row["status"] = "deletable"
            row["note"] = "force=True — will delete even if non-empty (any items → Default)"
        manifest.append(row)

    deletable = [m for m in manifest if m.get("status") == "deletable"]

    guard = _write_guard("delete_categories", category_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "force": force, "manifest": manifest}

    if dry_run:
        blocked = [m for m in manifest if m.get("status") == "blocked"]
        errors = [m for m in manifest if m.get("status") == "error"]
        return {
            "dry_run":    True,
            "force":      force,
            "item_count": len(category_ids),
            "manifest":   manifest,
            "message": (
                f"Dry run — nothing deleted. {len(deletable)} category(ies) would be "
                f"deleted, {len(blocked)} blocked, {len(errors)} error(s). Review the "
                "manifest, then set dry_run=False to execute."
            ),
        }

    results = []
    deleted = 0
    skipped_in_use = 0
    for m in deletable:
        cid = m["category_id"]
        err = None
        try:
            call_linnworks("Inventory/DeleteCategoryById", {"categoryId": cid})
        except RuntimeError as exc:
            err = str(exc)
        if err and _is_category_in_use(err):
            results.append({
                **m, "status": "skipped_in_use",
                "reason": (
                    "Linnworks reports this category is still in use, so it was NOT "
                    "deleted. It almost certainly holds ARCHIVED items (invisible to "
                    "the active-item sweep) or is referenced by a channel/listing "
                    "config. Unarchive/reassign those items (or clear the reference) "
                    "before it can be deleted."
                ),
                "detail": err,
            })
            skipped_in_use += 1
        elif err:
            results.append({**m, "delete_error": err})
        else:
            results.append({**m, "deleted": True})
            deleted += 1

    cats2 = _fetch_categories()
    still = {(c.get("CategoryId") or "").lower() for c in cats2}
    for r in results:
        if r.get("deleted") is True:
            r["deleted"] = (r["category_id"] or "").lower() not in still

    non_deletable = [m for m in manifest if m.get("status") != "deletable"]
    msg = f"Deleted {deleted} of {len(deletable)} deletable category(ies)."
    if skipped_in_use:
        msg += (
            f" {skipped_in_use} skipped — still in use (archived items or channel "
            "references; not truly empty)."
        )
    return {
        "dry_run":        False,
        "force":          force,
        "item_count":     len(category_ids),
        "deleted":        deleted,
        "skipped_in_use": skipped_in_use,
        "results":        results + non_deletable,
        "manifest":       manifest,
        "message":        msg,
    }


@_tool()
def delete_empty_categories(
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Find and PERMANENTLY delete every inventory category that has no items.

    This is the category-cleanup tool. It sweeps the entire catalogue (~2 min)
    to count items per category, identifies the ones with zero ACTIVE items,
    shows you a manifest, and — on a confirmed live run — deletes them. The
    built-in Default category is always excluded even if it is empty.

    ⚠️  "Empty" here = zero ACTIVE items. The sweep CANNOT see archived items,
    and no Linnworks endpoint counts archived items per category. So a category
    that holds only archived stock is flagged empty here but Linnworks will
    REFUSE to delete it ("Category is in use"). The tool handles this cleanly:
    such categories come back as `skipped_in_use` (not an error), and the
    response reports both `deleted` and `skipped_in_use` counts. To actually
    remove one, unarchive/reassign its items (or clear its channel reference)
    first. This means the empty_count is an UPPER BOUND on what will delete.

    This can surface a LOT of categories (archived ranges, junk from spreadsheet
    imports, seasonal lines you may want to keep) — so ALWAYS review the manifest
    first. If more than 10 categories are empty, you must re-call with
    confirmed_count=<N> to proceed, and the sweep re-runs on that call to
    re-verify emptiness. To delete only a hand-picked subset, copy their ids into
    delete_categories instead.

    dry_run=True by default. Uses Stock/GetStockItems (sweep) +
    Inventory/DeleteCategoryById (one call per empty category).

    Args:
        confirmed_count: For >10 empty categories, pass the empty_count reported
            in the manifest to confirm the deletion.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        A dict with empty_count, manifest (each empty category's id + name), and,
        on a live run, per-category delete results.
    """
    swept = _count_items_per_category()
    counts = swept["counts"]
    cats = _fetch_categories()

    empty = [
        c for c in cats
        if (c.get("CategoryId") or "").lower() != _DEFAULT_CATEGORY_ID
        and counts.get((c.get("CategoryId") or "").lower(), 0) == 0
    ]
    empty.sort(key=lambda c: (c.get("CategoryName") or "").lower())
    manifest = [
        {"category_id": c.get("CategoryId"), "category_name": c.get("CategoryName"), "item_count": 0}
        for c in empty
    ]

    guard = _write_guard("delete_empty_categories", empty, confirmed_count, dry_run)
    if guard is not None:
        return {
            **guard,
            "empty_count":  len(empty),
            "total_items":  swept["total_items"],
            "manifest":     manifest,
        }

    if dry_run:
        return {
            "dry_run":     True,
            "empty_count": len(empty),
            "total_items": swept["total_items"],
            "manifest":    manifest,
            "message": (
                f"Dry run — nothing deleted. {len(empty)} empty category(ies) found "
                f"(Default excluded). Review the manifest, then set dry_run=False to "
                "delete them."
            ),
        }

    results = []
    deleted = 0
    skipped_in_use = 0
    for c in empty:
        cid = c.get("CategoryId")
        err = None
        try:
            call_linnworks("Inventory/DeleteCategoryById", {"categoryId": cid})
        except RuntimeError as exc:
            err = str(exc)
        row = {"category_id": cid, "category_name": c.get("CategoryName")}
        if err and _is_category_in_use(err):
            row["status"] = "skipped_in_use"
            row["reason"] = (
                "Not truly empty — Linnworks reports it is still in use (ARCHIVED "
                "items, invisible to the active-item sweep, or a channel/listing "
                "reference). Not deleted."
            )
            row["detail"] = err
            skipped_in_use += 1
        elif err:
            row["delete_error"] = err
        else:
            row["deleted"] = True
            deleted += 1
        results.append(row)

    cats2 = _fetch_categories()
    still = {(c.get("CategoryId") or "").lower() for c in cats2}
    for r in results:
        if r.get("deleted") is True:
            r["deleted"] = (r["category_id"] or "").lower() not in still

    msg = f"Deleted {deleted} of {len(empty)} category(ies) reported empty by the sweep."
    if skipped_in_use:
        msg += (
            f" {skipped_in_use} skipped — still in use (archived items or channel "
            "references; the active-item sweep can't see those)."
        )
    return {
        "dry_run":        False,
        "empty_count":    len(empty),
        "deleted":        deleted,
        "skipped_in_use": skipped_in_use,
        "results":        results,
        "manifest":       manifest,
        "message":        msg,
    }


GLT_SHOPIFY_CHANNEL_TYPE = "Shopify"
GLT_SHOPIFY_CHANNEL_NAME = "SHOPIFY"
SHOPIFY_CONFIGURATOR_PROPERTY = "Shopify Configurator"
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"

PUSH_PROVEN = "proven"
PUSH_NEVER_ATTEMPTED = "never_attempted"
PUSH_ACCEPTED_NOT_PROCESSED = "accepted_but_not_processed"
PUSH_OBSERVED_STATES = (PUSH_PROVEN, PUSH_NEVER_ATTEMPTED,
                        PUSH_ACCEPTED_NOT_PROCESSED)

GLT_CHANNELS: dict[str, dict] = {
    "shopify": {"channel_type": "Shopify", "channel_name": "SHOPIFY", "source": "SHOPIFY",
                "delete_proven": True, "revise_proven": True,
                "push_observed_state": PUSH_PROVEN, "push_observed_reason": None},
    "amazon":  {"channel_type": "Amazon",  "channel_name": "AMAZON",  "source": "AMAZON",
                "delete_proven": True, "revise_proven": False,
                "push_observed_state": PUSH_ACCEPTED_NOT_PROCESSED,
                "push_observed_reason": (
                    "ProcessTemplates Revise/Update was fired live on Amazon twice "
                    "(24-25 Aug 2026, on two listings). Both "
                    "pushes were accepted (processed:true) and produced no "
                    "observable change on the listing -- the second, a price push on "
                    "an MFN standalone, never moved the offer price across an hour of "
                    "3-minute polling. This records what was observed on those two "
                    "listings at that time; it is not a proven rule about the API "
                    "path. revise_proven remains the single gate and stays False "
                    "until a push demonstrably lands."
                )},
    "tiktok":  {"channel_type": "TikTok",  "channel_name": "TIKTOK",  "source": "TIKTOK",
                "delete_proven": True, "revise_proven": False,
                "push_observed_state": PUSH_NEVER_ATTEMPTED, "push_observed_reason": None},
    "magento": {"channel_type": "Magento", "channel_name": "MAGENTO", "source": "MAGENTO",
                "delete_proven": False, "revise_proven": False,
                "push_observed_state": PUSH_NEVER_ATTEMPTED, "push_observed_reason": None},
    "walmart": {"channel_type": "Walmart", "channel_name": "WALMART", "source": "WALMART",
                "delete_proven": False, "revise_proven": False,
                "push_observed_state": PUSH_NEVER_ATTEMPTED, "push_observed_reason": None},
}

NON_GLT_SOURCES = ("EBAY", "MIRAKL MP", "ETSY", "CDISCOUNT")


def _proven_delete_channels() -> str:
    proven = sorted(c["channel_type"] for c in GLT_CHANNELS.values() if c["delete_proven"])
    return ", ".join(proven) if proven else "none"


def _proven_revise_channels() -> str:
    proven = sorted(c["channel_type"] for c in GLT_CHANNELS.values() if c["revise_proven"])
    return ", ".join(proven) if proven else "none"


EBAY_CHANNELS: dict[str, dict] = {
    "ebay": {
        "source": "EBAY",
        "revise_proven": False,
        "push_observed_state": PUSH_ACCEPTED_NOT_PROCESSED,
        "push_observed_reason": (
            "A live push was submitted against a real eBay listing on 26 Aug 2026 "
            "(a test listing) and accepted by Linnworks (2xx), "
            "but across a two-hour polling window on the description-frame URL no "
            "channel-side effect was observed; the identical edit pushed from the "
            "Linnworks listing UI landed within minutes. This records what was "
            "observed on that one listing at that time -- it is not a proven rule "
            "about the API path. revise_proven remains the single gate and stays "
            "False until a fresh proof lands."
        ),
    },
}

EBAY_UI_WORKAROUND_NOTE = (
    "The route that works today for propagating an eBay description edit is "
    "the Linnworks listing UI -- review this tool's dry-run manifest first to "
    "see what would be sent, then make the same edit there."
)


def _push_observation_reason(entry: dict) -> str:
    return entry.get("push_observed_reason") or ""


def _ebay_push_observation_reason() -> str:
    return _push_observation_reason(EBAY_CHANNELS["ebay"])


def _assert_push_observations_consistent() -> None:
    for name, registry in (("GLT_CHANNELS", GLT_CHANNELS),
                           ("EBAY_CHANNELS", EBAY_CHANNELS)):
        for key, entry in registry.items():
            state = entry.get("push_observed_state")
            if state not in PUSH_OBSERVED_STATES:
                raise ValueError(
                    f"{name}['{key}'].push_observed_state={state!r} is not one of "
                    f"{PUSH_OBSERVED_STATES}"
                )
            if entry.get("revise_proven") is not (state == PUSH_PROVEN):
                raise ValueError(
                    f"{name}['{key}'] contradicts itself: revise_proven="
                    f"{entry.get('revise_proven')!r} with push_observed_state="
                    f"{state!r}"
                )
            if state == PUSH_ACCEPTED_NOT_PROCESSED and not entry.get(
                    "push_observed_reason"):
                raise ValueError(
                    f"{name}['{key}'] is {PUSH_ACCEPTED_NOT_PROCESSED} but carries "
                    "no push_observed_reason"
                )


_assert_push_observations_consistent()


def _proven_ebay_revise_channels() -> str:
    proven = sorted(c["source"] for c in EBAY_CHANNELS.values() if c["revise_proven"])
    return ", ".join(proven) if proven else "none"


def _resolve_glt_channel(channel: str) -> dict:
    key = _norm_conf_name(channel).replace(" ", "")
    entry = GLT_CHANNELS.get(key)
    if entry is None:
        raise ValueError(
            f"'{channel}' is not a GLT-managed channel. Supported: "
            f"{sorted(c['channel_type'] for c in GLT_CHANNELS.values())}. "
            f"Channels like {', '.join(NON_GLT_SOURCES)} have no GLT templates, so they cannot "
            "be revised or deleted through this GLT machinery — they must be managed through "
            "that channel's own admin, or (eBay only) through "
            "revise_ebay_listing_description for description revises."
        )
    return {"key": key, **entry}


def _glt_field(info: dict, key: str):
    f = info.get(key)
    if isinstance(f, dict) and "Value" in f:
        return f.get("Value")
    return f


def _fetch_glt_configurators(channel: str = "Shopify") -> list[dict]:
    ch = _resolve_glt_channel(channel)
    resp = call_linnworks(
        "GenericListings/GetConfiguratorsInfoPaged",
        {"request": {
            "ChannelType": ch["channel_type"],
            "ChannelName": ch["channel_name"],
            "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 1000},
        }},
    )
    infos = resp.get("ConfiguratorsInfo") if isinstance(resp, dict) else None
    out: list[dict] = []
    for it in (infos or []):
        info = it.get("Info") if isinstance(it, dict) and isinstance(it.get("Info"), dict) else it
        out.append({
            "id":                _glt_field(info, "Id"),
            "name":              _glt_field(info, "Name"),
            "channel_id":        _glt_field(info, "ChannelId"),
            "sub_source":        _glt_field(info, "SubSource"),
            "show_in_inventory": _glt_field(info, "IsShowInInventory"),
        })
    return out


def _fetch_shopify_configurators() -> list[dict]:
    return _fetch_glt_configurators("Shopify")


def _norm_conf_name(name: str | None) -> str:
    return (name or "").strip().lower()


def _glt_channel_id_for(
    ss_to_channel: dict[str, int], sub_source: str
) -> tuple[int | None, str | None]:
    want = _norm_conf_name(sub_source)
    cid = ss_to_channel.get(want)
    if cid is not None:
        return cid, "exact"
    best: tuple[int, str] | None = None
    for acct, acct_cid in ss_to_channel.items():
        if acct and want.startswith(acct) and len(want) > len(acct):
            if best is None or len(acct) > len(best[1]):
                best = (acct_cid, acct)
    if best is not None:
        return best[0], "account-prefix"
    return None, None


def _resolve_glt_target(channel: str, sub_source: str) -> dict:
    ch = _resolve_glt_channel(channel)
    catalogue = _fetch_glt_configurators(ch["channel_type"])
    available = sorted({c["sub_source"] for c in catalogue if c.get("sub_source")})

    ss_to_channel: dict[str, int] = {}
    for c in catalogue:
        ss, cid = c.get("sub_source"), c.get("channel_id")
        if ss and cid is not None:
            ss_to_channel.setdefault(_norm_conf_name(ss), cid)

    base = {"channel": ch, "available_sub_sources": available}
    if not (sub_source or "").strip():
        return {
            **base, "ok": False, "channel_id": None, "resolution": None,
            "error": (
                f"sub_source is required: name the {ch['channel_type']} store/account to "
                f"act on, or set LINNWORKS_DEFAULT_SHOPIFY_SUB_SOURCE. Available: {available}"
            ),
        }
    cid, resolution = _glt_channel_id_for(ss_to_channel, sub_source)
    if cid is not None:
        return {**base, "ok": True, "channel_id": cid, "resolution": resolution}

    if not available:
        err = (
            f"{ch['channel_type']} has no GLT configurators in this account — nothing on "
            f"{ch['channel_type']} is GLT-managed here, so its listings cannot be taken down "
            "via the API."
        )
    else:
        err = (
            f"sub_source '{sub_source}' is not a known {ch['channel_type']} account/store in "
            f"this account. Available: {available}"
        )
    return {**base, "ok": False, "channel_id": None, "resolution": None, "error": err}


def _format_channel_sku_row(r: dict) -> dict:
    status = r.get("UpdateStatus")
    if isinstance(status, str) and len(status) > 280:
        status = status[:280] + " …(truncated)"
    return {
        "source":               r.get("Source"),
        "sub_source":           r.get("SubSource"),
        "channel_sku":          r.get("SKU"),
        "channel_reference_id": r.get("ChannelReferenceId"),
        "update_status":        status,
        "listed_quantity":      r.get("ListedQuantity"),
        "max_listed_quantity":  r.get("MaxListedQuantity"),
        "last_update":          r.get("LastUpdate"),
        "ignore_sync":          r.get("IgnoreSync"),
        "is_multi_location":    r.get("IsMultiLocation"),
        "channel_sku_row_id":   r.get("ChannelSKURowId"),
    }


def _fetch_channel_skus_for_ids(stock_item_ids: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    ids = [i for i in stock_item_ids if i]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "Inventory/BatchGetInventoryItemChannelSKUs",
            {"inventoryItemIds": chunk},
        )
        for entry in (resp if isinstance(resp, list) else []):
            sid = entry.get("StockItemId")
            if sid:
                out[sid.lower()] = entry.get("ChannelSkus") or []
    return out


_DANGLING_STATUS = "Not deleted"


def _dangling_verdict(status) -> str:
    if isinstance(status, str) and status.strip() == _DANGLING_STATUS:
        return "dangling_proven"
    return "not_proven_dangling"


def _template_row(t: dict) -> dict:
    info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
    status = _glt_field(info, "Status")
    raw_id = t.get("Id")
    try:
        template_id = int(raw_id)
    except (TypeError, ValueError):
        template_id = raw_id
    return {
        "template_id":       template_id,
        "configurator_id":   t.get("ConfiguratorId"),
        "active_listing_id": _glt_field(info, "ActiveListingId"),
        "status":            status,
        "verdict":           _dangling_verdict(status),
    }


def _open_item_templates(ch: dict, channel_id: int, stock_item_id: str) -> list[dict]:
    resp = call_linnworks(
        "GenericListings/OpenTemplatesByInventory",
        {"request": {
            "ChannelType": ch["channel_type"],
            "ChannelName": ch["channel_name"],
            "Parameters": {
                "SelectedRegions": [],
                "Token": _ZERO_GUID,
                "InventoryItemIds": [stock_item_id],
                "ChannelId": channel_id,
            },
            "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 20},
        }},
    )
    return (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []


@_tool()
def find_dangling_glt_templates(
    skus: list[str],
    sub_source: str = DEFAULT_SHOPIFY_SUB_SOURCE,
    channel: str = "Shopify",
) -> dict:
    """
    Report which of a SKU's GLT templates are PROVABLY dangling — pointing at a
    listing that no longer exists on the channel. Read-only; writes nothing.

    A dangling template is invisible to `get_channel_listings`: the channel-SKU
    mapping can look perfectly healthy while the template's stored
    ActiveListingId points at a deleted product. It breaks pushes in a nasty way
    (Linnworks batches listing ids into one Shopify `nodes(ids:)` call and throws
    on the null slot, failing every healthy template batched with it).

    ⚠️  THIS CAN PROVE A TEMPLATE IS DANGLING. IT CAN NEVER PROVE ONE IS FINE.
    Proof of death is `Info.Status == "Not deleted"`, which is Linnworks
    recording that it tried to remove a listing already gone. That signal is
    precise (141/141 and 42/42 across two sweeps) but not complete (~4 of 145
    dangling templates carry another status; "Listed" templates have never been
    swept). So the verdicts are `dangling_proven` and `not_proven_dangling` —
    the second means UNPROVEN, not healthy. There is no `healthy` verdict.

    This is a VERIFICATION tool, not a discovery tool: scope is per-SKU, so you
    must already know which SKU to ask about. Discovery is the catalogue sweep
    whose output is a sweep script of your own.

    Remediation for a dangling template is a HUMAN step in the Linnworks GLT UI
    (open the listing so the template re-points at the surviving product). See
    `delete_dangling_glt_template` for the provisional, experimental API route
    and why it may not work.

    Args:
        skus:       SKUs to examine.
        sub_source: Store/account name, e.g. "My Shopify Store".
        channel:    "Shopify" (default), "Amazon", "TikTok", Magento, Walmart.

    Returns:
        items[] (per SKU: templates with verdicts, channel-SKU rows,
        template_source/parent_sku for a variation child, remediation),
        unresolved[], rate_limited[], the two counts, items_examined,
        complete, and detector_note.
    """
    target = _resolve_glt_target(channel, sub_source)
    if not target.get("ok"):
        return {"success": False, "error": target.get("error"),
                "available_sub_sources": target.get("available_sub_sources")}

    ch = target["channel"]
    channel_id = target["channel_id"]
    channel_source = ch["source"]

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    items: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []

    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "blocked_reason": "empty_sku", "error": "empty SKU"})
            continue
        _check_injection("skus", sku)
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": f"not found: {exc}"})
            continue

        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": "item found but StockItemId was missing"})
            continue

        try:
            raw_templates = _open_item_templates(ch, channel_id, sid)
            rows = _rows_on_store(_fetch_channel_skus_for_ids([sid]).get(sid.lower(), []))
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "channel_read_failed",
                               "error": str(exc)})
            continue

        template_source = "item"
        parent_sku = None
        note = None

        if not raw_templates:
            try:
                rel = _resolve_variation(sku, sid)
            except RateLimitError as exc:
                rate_limited.append({"sku": sku, "error": str(exc)})
                continue
            except RuntimeError as exc:
                unresolved.append({"sku": sku, "blocked_reason": "channel_read_failed",
                                   "error": f"variation lookup failed: {exc}"})
                continue
            if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
                parent_sku = rel.get("parent_sku")
                try:
                    raw_templates = _open_item_templates(
                        ch, channel_id, rel["parent_stock_item_id"])
                except RateLimitError as exc:
                    rate_limited.append({"sku": sku, "error": str(exc)})
                    continue
                except RuntimeError as exc:
                    unresolved.append({"sku": sku, "blocked_reason": "channel_read_failed",
                                       "error": str(exc)})
                    continue
                template_source = "variation_parent"
                note = (f"variation child — the group's template(s) hang off parent "
                        f"'{parent_sku}' and serve every member")

        rows_out = [_format_channel_sku_row(r) for r in rows]
        template_rows = [_template_row(t) for t in raw_templates]
        if not template_rows:
            template_source = "none"
            note = note or (
                f"no GLT template on {ch['channel_type']} / {sub_source} for this SKU — "
                "nothing to report either way; this is NOT a clean bill of health for the SKU")

        items.append({
            "sku": sku,
            "stock_item_id": sid,
            "title": item.get("ItemTitle"),
            "channel": ch["channel_type"],
            "sub_source": sub_source,
            "channel_id": channel_id,
            "template_source": template_source,
            "parent_sku": parent_sku,
            "no_templates": not template_rows,
            "templates": template_rows,
            "dangling_proven": [t["template_id"] for t in template_rows
                                if t["verdict"] == "dangling_proven"],
            "channel_sku_rows": {
                "count": len(rows_out),
                "channel_reference_ids": [r["channel_reference_id"] for r in rows_out],
                "rows": rows_out,
            },
            "remediation": (
                "Rebuild or remove the template in the Linnworks GLT UI — opening the "
                "listing re-points the template at the surviving product. This is the "
                "human step v1.50.0 recommended; the API route is provisional, see "
                "delete_dangling_glt_template."
            ),
            "note": note,
        })

    proven = sum(len(i["dangling_proven"]) for i in items)
    total = sum(len(i["templates"]) for i in items)
    return {
        "success": True,
        "channel": ch["channel_type"],
        "sub_source": sub_source,
        "sub_source_resolution": target.get("resolution"),
        "items": items,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
        "items_examined": len(items),
        "dangling_proven_count": proven,
        "not_proven_dangling_count": total - proven,
        "complete": not unresolved and not rate_limited,
        "detector_note": (
            "PRECISION, NOT RECALL. `Not deleted` proves a template is dangling "
            "(141/141 and 42/42 across two sweeps). It does NOT prove the others are "
            "fine — ~4 of 145 known dangling templates carry another status, and "
            "`Listed` templates have never been swept. `not_proven_dangling` means "
            "UNPROVEN, not a clean bill of health."
        ),
    }


def _rows_kept_across(before_by_id: dict, after_by_id: dict) -> bool:
    return all(
        before_by_id.get(i, 0) == 0 or after_by_id.get(i, 0) >= before_by_id.get(i, 0)
        for i in before_by_id
    )


def _reference_ids_kept_across(before_refs_by_id: dict, after_refs_by_id: dict) -> bool:
    return all(
        before_refs_by_id.get(i, set()) <= after_refs_by_id.get(i, set())
        for i in before_refs_by_id
    )


def _append_warning(existing: str | None, addition: str) -> str:
    return f"{existing}\n\n{addition}" if existing else addition


@_tool()
def delete_dangling_glt_template(
    sku: str,
    template_id: int,
    sub_source: str = DEFAULT_SHOPIFY_SUB_SOURCE,
    channel: str = "Shopify",
    allow_unproven_delete: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    ⚠️ PROVISIONAL AND EXPERIMENTAL. Delete ONE named GLT template whose listing
    is already gone, leaving the live listing beside it alone. It exists to answer
    a question on one SKU, not for cleanup at scale; run it on ONE low-value SKU,
    record the result, and never batch it.

    Two open questions, in order:
      A. Does Delete land at all on a template reading "Not deleted"? Unknown:
         that status means Linnworks already tried and failed, and re-firing
         Delete on such templates has returned success while changing nothing.
      B. If it lands, does it take the ITEM's channel-SKU rows, and so the LIVE
         sibling's mapping, with it? Those rows belong to the item, not the
         template. If so, the sibling listing stays up but silently loses stock
         and price sync.

    Refused, with no write, in this order:
      template_not_on_item          the id isn't a template of this SKU here
      dangling_not_proven           the status isn't exactly "Not deleted"
      variation_child_live_siblings the group's template serves live (or
                                    unverifiable, see `unknown_siblings`) members
      no_sibling_use_unpublish      only one template: use unpublish_channel_listing
      before_snapshot_incomplete    the before-write read left out an affected
                                    item entirely, which would disable the
                                    mapping-lost check

    Live outcome: orphan_removed_siblings_intact,
    orphan_removed_sibling_mapping_lost (failure: restore the mapping in the UI),
    orphan_removed_sibling_template_lost (worse: a sibling's template is gone),
    delete_refused_by_linnworks, or unconfirmed (treat as "may have happened").

    Args:
        sku: ONE SKU. No lists, by design.
        template_id: ONE template id, belonging to that SKU on this store.
        sub_source: The store or account.
        channel: "Shopify" (default), "Amazon", "TikTok", …
        allow_unproven_delete: Skip the "Not deleted" check and delete a template
            not proven dead, on an item with a live sibling, which is the exact
            risk this tool guards against. Recorded as a warning.
        confirmed_count: Echo back 1 to execute a live run (always staged).
        dry_run: Default True: plans and checks, writes nothing.

    Returns:
        plan (target and siblings_untouched), before/after snapshots, outcome; on
        a refusal, success=False with blocked_reason.
    """
    _check_injection("sku", sku or "")
    target = _resolve_glt_target(channel, sub_source)
    if not target.get("ok"):
        return {"success": False, "error": target.get("error"),
                "available_sub_sources": target.get("available_sub_sources")}

    ch = target["channel"]
    channel_id = target["channel_id"]
    channel_source = ch["source"]

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    base = {"success": False, "sku": sku, "template_id": template_id,
            "channel": ch["channel_type"], "sub_source": sub_source,
            "dry_run": dry_run}

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {**base, "blocked_reason": "not_found", "error": f"not found: {exc}"}

    sid = item.get("StockItemId")
    if not sid:
        return {**base, "blocked_reason": "not_found",
                "error": "item found but StockItemId was missing"}

    template_sid, via_parent, group = sid, False, None
    try:
        raw_templates = _open_item_templates(ch, channel_id, sid)
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {**base, "blocked_reason": "channel_read_failed", "error": str(exc)}

    if not raw_templates:
        try:
            rel = _resolve_variation(sku, sid)
        except RateLimitError as exc:
            return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
        except RuntimeError as exc:
            return {**base, "blocked_reason": "channel_read_failed",
                    "error": f"variation lookup failed: {exc}"}
        if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
            group, via_parent = rel, True
            template_sid = rel["parent_stock_item_id"]
            try:
                raw_templates = _open_item_templates(ch, channel_id, template_sid)
            except RateLimitError as exc:
                return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
            except RuntimeError as exc:
                return {**base, "blocked_reason": "channel_read_failed", "error": str(exc)}

    rows = [_template_row(t) for t in raw_templates]

    try:
        wanted = int(template_id)
    except (TypeError, ValueError):
        return {**base, "blocked_reason": "template_not_on_item",
                "error": f"template_id {template_id!r} is not a template id."}

    def _row_matches(r: dict) -> bool:
        if r["template_id"] is None:
            return False
        try:
            return int(r["template_id"]) == wanted
        except (TypeError, ValueError):
            return False

    target_row = next((r for r in rows if _row_matches(r)), None)
    if target_row is None:
        return {**base, "blocked_reason": "template_not_on_item",
                "templates_on_item": [r["template_id"] for r in rows],
                "error": (f"template {template_id} is not among this SKU's templates on "
                          f"{ch['channel_type']} / {sub_source}. A template id from another "
                          "SKU or store is never deleted by this tool.")}

    siblings = [r for r in rows if r["template_id"] != wanted]

    warning = None
    if target_row["verdict"] != "dangling_proven":
        if not allow_unproven_delete:
            return {**base, "blocked_reason": "dangling_not_proven",
                    "status": target_row["status"],
                    "error": (f"template {wanted} reads status {target_row['status']!r}, not "
                              f"{_DANGLING_STATUS!r}. Only a template PROVEN dangling may be "
                              "deleted. Pass allow_unproven_delete=True to override — that "
                              "fires a delete at a listing that may still be live.")}
        warning = (f"OVERRIDE: template {wanted} is NOT proven dead (status "
                   f"{target_row['status']!r}). If listing {target_row['active_listing_id']} "
                   "is still live on the channel, this will end it.")

    if via_parent and group:
        members = [{"sku": group.get("parent_sku"), "stock_item_id": group.get("parent_stock_item_id")}]
        members += [{"sku": s.get("sku"), "stock_item_id": s.get("stock_item_id")}
                    for s in (group.get("siblings") or []) if s.get("sku")]
        outside = [m for m in members
                   if (m["sku"] or "").strip().lower() != (sku or "").strip().lower()]
        ids = [m["stock_item_id"] for m in outside if m.get("stock_item_id")]
        try:
            sib_map = _fetch_channel_skus_for_ids(ids) if ids else {}
        except RateLimitError as exc:
            return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
        except RuntimeError as exc:
            return {**base, "blocked_reason": "channel_read_failed", "error": str(exc)}

        unknown = [m["sku"] for m in outside if not m.get("stock_item_id")]
        unknown += [m["sku"] for m in outside
                    if m.get("stock_item_id")
                    and (m["stock_item_id"] or "").lower() not in sib_map]
        live_outside = [m["sku"] for m in outside
                        if m.get("stock_item_id")
                        and (m["stock_item_id"] or "").lower() in sib_map
                        and _rows_on_store(sib_map[(m["stock_item_id"] or "").lower()])]
        if unknown:
            return {**base, "blocked_reason": "variation_child_live_siblings",
                    "parent_sku": group.get("parent_sku"),
                    "group_name": group.get("group_name"),
                    "live_siblings": live_outside,
                    "unknown_siblings": unknown,
                    "error": ("this template hangs off the variation parent and serves every "
                              f"member; the live status of {len(unknown)} other member(s) "
                              f"({', '.join(unknown)}) could not be confirmed, so this refuses "
                              "rather than assume an unreadable sibling is not live.")}
        if live_outside:
            return {**base, "blocked_reason": "variation_child_live_siblings",
                    "parent_sku": group.get("parent_sku"),
                    "group_name": group.get("group_name"),
                    "live_siblings": live_outside,
                    "error": ("this template hangs off the variation parent and serves every "
                              f"member; {len(live_outside)} other member(s) are still live on "
                              "this store, so deleting it would end their listing too.")}

    if not siblings:
        return {**base, "blocked_reason": "no_sibling_use_unpublish",
                "error": ("this item has only one template, so there is no sibling for this "
                          "tool to protect and its whole purpose does not apply. Use "
                          "unpublish_channel_listing, which is live-proven for that shape.")}

    snapshot_ids = sorted({sid, template_sid})
    try:
        before_snap = _fetch_channel_skus_for_ids(snapshot_ids)
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {**base, "blocked_reason": "channel_read_failed", "error": str(exc)}

    missing_before = [i for i in snapshot_ids if i.lower() not in before_snap]
    if missing_before:
        return {**base, "blocked_reason": "before_snapshot_incomplete",
                "missing_stock_item_ids": missing_before,
                "error": (f"the channel-SKU batch read did not return an entry for "
                          f"{missing_before} before any write — an omission cannot be told "
                          "apart from a confirmed zero, and a confirmed zero is exempt from "
                          "the mapping-lost check. Refusing rather than write blind.")}

    before_by_id = {i: len(before_snap.get(i.lower(), [])) for i in snapshot_ids}
    before_ref_ids_by_id = {
        i: {r.get("ChannelReferenceId") for r in before_snap.get(i.lower(), [])
            if r.get("ChannelReferenceId")}
        for i in snapshot_ids
    }
    before_rows = [_format_channel_sku_row(r) for i in snapshot_ids
                   for r in _rows_on_store(before_snap.get(i.lower(), []))]

    before = {
        "channel_sku_row_count": len(before_rows),
        "channel_reference_ids": [r["channel_reference_id"] for r in before_rows],
        "templates": {r["template_id"]: r["status"] for r in rows},
        "by_stock_item_id": before_by_id,
    }
    plan = {
        "template_id": wanted,
        "active_listing_id": target_row["active_listing_id"],
        "status": target_row["status"],
        "action": "Delete",
        "via_variation_parent": via_parent,
        "siblings_untouched": siblings,
    }
    out = {**base, "success": True, "template_stock_item_id": template_sid,
           "plan": plan, "before": before, "warning": warning}

    if dry_run:
        out["complete"] = True
        out["message"] = (
            f"DRY RUN — nothing sent. Would delete template {wanted} and leave "
            f"{len(siblings)} sibling template(s) in place. Re-run with dry_run=False "
            "and confirmed_count=1 to execute.")
        return out

    guard = _write_guard("delete_dangling_glt_template", [plan], confirmed_count, dry_run)
    if guard:
        return {**out, **guard, "plan": plan}

    out["dry_run"] = False
    try:
        call_linnworks(
            "GenericListings/ProcessTemplates",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "TemplateRequests": [{"TemplateId": wanted, "Action": "Delete"}],
                "ClientContext": {"Activity": "delete_dangling_glt_template",
                                  "Source": "linnworks-plugin"},
            }},
        )
        out["processed"] = True
    except RateLimitError as exc:
        return {**out, "success": False, "processed": False, "outcome": "unconfirmed",
                "settles": "neither", "complete": False,
                "error": f"ProcessTemplates (Delete) failed: {exc}",
                "warning": _append_warning(
                    out.get("warning"),
                    "the request was rate-limited and never reached Linnworks's processing "
                    "— not sent, safe to retry once the quota window clears.")}
    except (RuntimeError, OSError) as exc:
        return {**out, "success": False, "processed": "unknown", "outcome": "unconfirmed",
                "settles": "neither", "complete": False,
                "error": f"ProcessTemplates (Delete) failed: {exc}",
                "warning": _append_warning(
                    out.get("warning"),
                    "THE REQUEST MAY HAVE REACHED LINNWORKS AND ALREADY APPLIED — this was "
                    "either a non-2xx response (the request WAS received and evaluated) or a "
                    "transport failure (timeout/connection drop) AFTER the POST was sent. Do "
                    "NOT assume nothing happened. Re-open this item's templates and read its "
                    "channel-SKU rows by hand before doing anything else, including retrying.")}

    try:
        after_snap = _fetch_channel_skus_for_ids(snapshot_ids)
        after_by_id = {i: len(after_snap.get(i.lower(), [])) for i in snapshot_ids}
        after_ref_ids_by_id = {
            i: {r.get("ChannelReferenceId") for r in after_snap.get(i.lower(), [])
                if r.get("ChannelReferenceId")}
            for i in snapshot_ids
        }
        after_rows_raw = [r for i in snapshot_ids
                          for r in _rows_on_store(after_snap.get(i.lower(), []))]
        after_templates = [_template_row(t) for t in
                           _open_item_templates(ch, channel_id, template_sid)]
    except (RateLimitError, RuntimeError, OSError) as exc:
        return {**out, "success": False, "outcome": "unconfirmed", "settles": "neither",
                "complete": False, "error": str(exc),
                "warning": _append_warning(
                    out.get("warning"),
                    "THE DELETE WAS ALREADY SENT and may have succeeded — the "
                    "read-back could not complete. Do NOT re-run on the assumption "
                    "it failed. Re-open this item's templates and read its "
                    "channel-SKU rows by hand before doing anything else.")}

    after_rows = [_format_channel_sku_row(r) for r in after_rows_raw]
    out["after"] = {
        "channel_sku_row_count": len(after_rows),
        "channel_reference_ids": [r["channel_reference_id"] for r in after_rows],
        "templates": {r["template_id"]: r["status"] for r in after_templates},
        "by_stock_item_id": after_by_id,
    }
    out["complete"] = True

    after_templates_by_id = out["after"]["templates"]
    target_gone = wanted not in after_templates_by_id
    siblings_before_ids = [r["template_id"] for r in siblings]
    siblings_present = [tid for tid in siblings_before_ids if tid in after_templates_by_id]
    siblings_lost = [tid for tid in siblings_before_ids if tid not in after_templates_by_id]
    out["siblings_still_present"] = siblings_present

    rows_kept = (_rows_kept_across(before_by_id, after_by_id)
                and _reference_ids_kept_across(before_ref_ids_by_id, after_ref_ids_by_id))
    siblings_not_proven_dangling = any(r["verdict"] == "not_proven_dangling" for r in siblings)
    out["siblings_not_proven_dangling"] = siblings_not_proven_dangling

    before_all_zero = all(v == 0 for v in before_by_id.values())
    before_store_zero = len(before_rows) == 0
    grown_ids = [i for i in before_by_id if after_by_id.get(i, 0) > before_by_id.get(i, 0)]

    notes: list[str] = []

    if not target_gone:
        out.update({
            "success": False, "outcome": "delete_refused_by_linnworks", "settles": "A only",
            "message": (
                f"Template {wanted} is STILL PRESENT, reading "
                f"{after_templates_by_id.get(wanted)!r}. The delete did not land. This "
                "answers question A (does Delete work on a dangling template) with NO, and "
                "leaves question B (does it take the sibling's channel-SKU rows) untouched. "
                "The GLT UI is the remediation route."),
        })
    elif siblings_lost:
        out.update({
            "success": False, "outcome": "orphan_removed_sibling_template_lost",
            "settles": "A and B",
            "warning": _append_warning(out.get("warning"), (
                f"⛔⛔ A SIBLING TEMPLATE IS GONE, not just its rows. Template {wanted} was "
                f"deleted and sibling template id(s) {siblings_lost} are no longer present on "
                f"this item at all — the read-back now shows {after_templates_by_id!r}. This "
                "is worse than a lost channel-SKU mapping: the sibling's own LISTING TEMPLATE "
                "may have just been removed by this delete. Treat the sibling as potentially "
                "dead, check it on the channel immediately, and do NOT run this tool again "
                "until this is understood.")),
        })
    elif not rows_kept:
        per_id = "; ".join(
            f"{i}: {before_by_id.get(i, 0)} → {after_by_id.get(i, 0)}" for i in snapshot_ids)
        out.update({
            "success": False, "outcome": "orphan_removed_sibling_mapping_lost",
            "settles": "A and B",
            "warning": _append_warning(out.get("warning"), (
                f"⛔ THE LIVE LISTING HAS lost its Linnworks mapping. Template {wanted} was "
                f"deleted and channel-SKU rows per stock item (item-wide, not filtered to "
                f"this store) went from {per_id}. The sibling listing "
                "is probably still up on the channel but no longer syncs stock or price, so "
                "it can oversell with nothing flagging it. Restore the mapping via the "
                "Linnworks UI and do NOT run this tool on any other SKU.")),
        })
    else:
        settles = "A and B"
        if before_all_zero:
            settles = "A only"
            notes.append(
                "every snapshotted stock item had ZERO channel-SKU rows before the delete "
                "(item-wide, across every channel), so this run demonstrates question A only "
                "(Delete lands on a dangling template) — question B (does it also take the "
                "sibling's rows) is UNTESTABLE on this SKU; rows that were never there cannot "
                "be shown to survive.")
        if before_store_zero:
            settles = "A only"
            notes.append(
                "this store had ZERO channel-SKU rows before the delete for the snapshotted "
                "stock item(s), so this run demonstrates question A only (Delete lands on a "
                "dangling template) — question B (does it also take the LIVE sibling's "
                "mapping ON THIS STORE) is UNTESTABLE on this SKU, even where the item has "
                "rows on another channel; a mapping that was never there on this store cannot "
                "be shown to survive.")
        out.update({
            "success": True, "outcome": "orphan_removed_siblings_intact", "settles": settles,
            "message": (
                f"Template {wanted} deleted; sibling template(s) {siblings_present} still "
                f"present and the item's channel-SKU rows intact — checked item-wide, not "
                f"just this store ({len(after_rows)} row(s) on this store specifically). "
                "Verify the sibling's product page on the channel, and check again after "
                "the next stock sync that quantity still updates."),
        })

    if not siblings_not_proven_dangling:
        notes.append(
            "no sibling on this item was proven live — every sibling template read as "
            "dangling too, so this run does not demonstrate that a LIVE listing survives.")
    if grown_ids:
        notes.append(
            f"channel-SKU row count ROSE after the delete on stock item(s) {grown_ids} — "
            "rows appearing after a delete is anomalous; the count/reference-id checks read "
            "this as \"kept\", but it is worth a manual look.")
    if notes:
        out["note"] = "\n\n".join(notes)
    return out


@_tool()
def get_channel_listings(sku: str) -> dict:
    """
    Read the existing channel listings for ONE inventory item — answers
    "is this SKU already listed, and on which channel/store?".

    Reads the Linnworks channel-SKU link table
    (Inventory/GetInventoryItemChannelSKUs) for the item. Each row is a live
    mapping between this stock item and a sales-channel listing, so a non-empty
    result means the item is already listed/linked on that channel. Covers every
    channel the item is mapped to — Shopify (per store), eBay, Amazon (per
    region), Mirakl, Magento, etc.

    This is the read-side companion to `list_to_shopify`: use it to confirm
    whether a SKU is already live before (re)listing, instead of guessing from
    the barcode. To check many SKUs at once, use `get_channel_listings_bulk`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - is_listed:     True if the item has any channel-SKU mapping
          - listing_count: number of channel listings
          - channels:      distinct Source values (e.g. ["AMAZON", "SHOPIFY"])
          - sub_sources:   distinct store/region names (e.g. ["My Shopify Store"])
          - listings:      per-channel rows (source, sub_source, channel_sku,
                           channel_reference_id, update_status, listed_quantity,
                           max_listed_quantity, last_update, ignore_sync,
                           is_multi_location, channel_sku_row_id)

    Note: Linnworks does not return a ready-made listing URL. `channel_sku` and
    `channel_reference_id` identify the listing on the channel (for Shopify the
    reference is a product:variant:inventory id triple).
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemChannelSKUs",
        {"inventoryItemId": stock_item_id},
    )
    listings = [_format_channel_sku_row(r) for r in (rows if isinstance(rows, list) else [])]

    return {
        "sku":           sku,
        "stock_item_id": stock_item_id,
        "title":         item.get("ItemTitle"),
        "is_listed":     len(listings) > 0,
        "listing_count": len(listings),
        "channels":      sorted({l["source"] for l in listings if l.get("source")}),
        "sub_sources":   sorted({l["sub_source"] for l in listings if l.get("sub_source")}),
        "listings":      listings,
    }


def _resolve_bulk_inputs(
    skus: list[str] | None,
    stock_item_ids: list[str] | None,
) -> tuple[list[tuple[str, str]], dict[str, str], list[dict], list[dict]]:
    resolved: list[tuple[str, str]] = []
    titles: dict[str, str] = {}
    unresolved: list[dict] = []
    rate_limited: list[dict] = []

    for raw in (stock_item_ids or []):
        sid = (raw or "").strip()
        if not sid:
            unresolved.append({"stock_item_id": raw, "error": "empty stock_item_id"})
            continue
        resolved.append((sid, sid))

    for raw in (skus or []):
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
        except RateLimitError as exc:
            rate_limited.append({"sku": s, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": s, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
            continue
        resolved.append((s, sid))
        titles[sid.lower()] = item.get("ItemTitle")

    return resolved, titles, unresolved, rate_limited


@_tool()
def get_channel_listings_bulk(
    skus: list[str] | None = None,
    stock_item_ids: list[str] | None = None,
) -> dict:
    """
    Read the existing channel listings for MANY inventory items at once — the
    batch dedupe companion to `list_to_shopify`.

    Reads the channel-SKU link table via Inventory/BatchGetInventoryItemChannelSKUs
    (one batched call per 200 items). Use before a bulk listing run to skip SKUs
    already live on a channel/store, or before a delist/archive sweep to find what
    is still live.

    ⚠️  PREFER `stock_item_ids` FOR LARGE BATCHES. Resolving SKUs costs one
    `GetInventoryItem` call each against a 150/min quota, so a few thousand SKUs
    will spend minutes in backoff; the batched read itself is ~1 call per 200.
    Anything chaining from `list_inventory_items` or `find_composite_parents`
    already has the ids. Measured: 5,391 items took 187s (and was wrong) by SKU
    vs 15.6s / 27 calls by id.

    ⚠️  `unresolved` means the catalogue has no such SKU. Rate-limited lookups go
    to a SEPARATE `rate_limited` list — they are transient and should be retried,
    NOT treated as missing items. Conflating the two previously under-reported
    live listings by 88%.

    Args:
        skus: Exact SKUs / ItemNumbers to check.
        stock_item_ids: StockItemId GUIDs to check (skips resolution — faster and
            quota-free). Either or both may be given.

    Returns:
        A dict with:
          - item_count:     number of inputs requested
          - resolved_count: how many resolved to a stock item
          - listed_count / unlisted_count: split of resolved items by whether
            they have any channel listing
          - results: per-item rows (sku, stock_item_id, title, is_listed,
            listing_count, channels, sub_sources, listings) — same listing shape
            as get_channel_listings
          - unresolved: rows for inputs the catalogue does not have
          - rate_limited: rows that hit the quota; re-run these
          - complete: False when anything was rate-limited (the answer is partial)
    """
    if not skus and not stock_item_ids:
        raise ValueError("Provide at least one of skus or stock_item_ids.")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, stock_item_ids)

    by_id = _fetch_channel_skus_for_ids([sid for _, sid in resolved])

    results: list[dict] = []
    listed_count = 0
    for s, sid in resolved:
        listings = [_format_channel_sku_row(r) for r in by_id.get(sid.lower(), [])]
        if listings:
            listed_count += 1
        results.append({
            "sku":           s,
            "stock_item_id": sid,
            "title":         titles.get(sid.lower()),
            "is_listed":     len(listings) > 0,
            "listing_count": len(listings),
            "channels":      sorted({l["source"] for l in listings if l.get("source")}),
            "sub_sources":   sorted({l["sub_source"] for l in listings if l.get("sub_source")}),
            "listings":      listings,
        })

    return {
        "item_count":     len(skus or []) + len(stock_item_ids or []),
        "resolved_count": len(resolved),
        "listed_count":   listed_count,
        "unlisted_count": len(resolved) - listed_count,
        "results":        results,
        "unresolved":     unresolved,
        "rate_limited":   rate_limited,
        "complete":       not rate_limited,
    }


def _format_image_row(r: dict) -> dict:
    return {
        "image_id":      r.get("pkRowId"),
        "is_main":       bool(r.get("IsMain")),
        "sort_order":    r.get("SortOrder"),
        "full_url":      r.get("FullSource"),
        "thumbnail_url": r.get("Source") or r.get("FullSourceThumbnail"),
    }


def _fetch_images_for_ids(stock_item_ids: list[str]) -> dict[str, list]:
    out: dict[str, list] = {}
    ids = [i for i in stock_item_ids if i]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "Inventory/GetImagesInBulk",
            {"request": {"StockItemIds": chunk}},
        )
        imgs = resp.get("Images") if isinstance(resp, dict) else None
        for img in (imgs if isinstance(imgs, list) else []):
            sid = img.get("StockItemId")
            if sid:
                out.setdefault(sid.lower(), []).append(img)
    return out


@_tool()
def get_inventory_item_images(sku: str) -> dict:
    """
    Read the images attached to ONE inventory item — answers "does this SKU have
    a product image, how many, and is a main image set?".

    Reads the Linnworks image table (Inventory/GetInventoryItemImages) for the
    item. This is the read-side companion to `add_inventory_item_images` (the
    write tool) and the third piece of the safe-bulk-listing workflow alongside
    `get_channel_listings` (is it already listed?) and `list_to_shopify`.

    Use it as a pre-listing image gate: an item with no image — or one with
    pictures but no main image set — is not ready to go live on a channel.
    To check many SKUs at once, use `get_inventory_item_images_bulk`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - has_image:      True if the item has at least one image
          - image_count:    number of images
          - has_main_image: True if any image is flagged as the main image —
                            spot items that have a picture but no main set
          - images:         per-image rows (image_id, is_main, sort_order,
                            full_url, thumbnail_url), sorted by sort_order
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemImages",
        {"inventoryItemId": stock_item_id},
    )
    images = [_format_image_row(r) for r in (rows if isinstance(rows, list) else [])]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":            sku,
        "stock_item_id":  stock_item_id,
        "title":          item.get("ItemTitle"),
        "has_image":      len(images) > 0,
        "image_count":    len(images),
        "has_main_image": any(im["is_main"] for im in images),
        "images":         images,
    }


@_tool()
def get_inventory_item_images_bulk(
    skus: list[str] | None = None,
    stock_item_ids: list[str] | None = None,
) -> dict:
    """
    Read the images for MANY inventory items at once — the batch pre-listing
    image gate, mirroring `get_channel_listings_bulk`.

    Batch-reads the image table via Inventory/GetImagesInBulk (one batched call
    per 200 items). An item that exists but has zero images is reported with
    has_image=False (a real item to fix), distinct from an `unresolved` input
    (not in the catalogue).

    Use this before a bulk listing run to skip/flag SKUs with no image, so you
    only list the genuinely ready ones.

    ⚠️  PREFER `stock_item_ids` FOR LARGE BATCHES — resolving SKUs costs one
    `GetInventoryItem` call each against a 150/min quota, while the batched read
    is ~1 call per 200. See `get_channel_listings_bulk`.

    ⚠️  `unresolved` means the catalogue has no such SKU; rate-limited lookups go
    to a separate `rate_limited` list and should be retried, never read as
    missing items.

    Args:
        skus: Exact SKUs / ItemNumbers to check.
        stock_item_ids: StockItemId GUIDs to check (skips resolution). Either or
            both may be given.

    Returns:
        A dict with:
          - item_count:        number of inputs requested
          - resolved_count:    how many resolved to a stock item
          - with_image_count / without_image_count: split of resolved items by
            whether they have any image
          - results: per-item rows (sku, stock_item_id, title, has_image,
            image_count, has_main_image, images) — same image shape as
            get_inventory_item_images
          - unresolved: rows for inputs the catalogue does not have
          - rate_limited: rows that hit the quota; re-run these
          - complete: False when anything was rate-limited (the answer is partial)
    """
    if not skus and not stock_item_ids:
        raise ValueError("Provide at least one of skus or stock_item_ids.")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, stock_item_ids)

    by_id = _fetch_images_for_ids([sid for _, sid in resolved])

    results: list[dict] = []
    with_image = 0
    for s, sid in resolved:
        raw_imgs = by_id.get(sid.lower(), [])
        images = [_format_image_row(r) for r in raw_imgs]
        images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))
        if images:
            with_image += 1
        results.append({
            "sku":            s,
            "stock_item_id":  sid,
            "title":          titles.get(sid.lower()),
            "has_image":      len(images) > 0,
            "image_count":    len(images),
            "has_main_image": any(im["is_main"] for im in images),
            "images":         images,
        })

    return {
        "item_count":         len(skus or []) + len(stock_item_ids or []),
        "resolved_count":     len(resolved),
        "with_image_count":   with_image,
        "without_image_count": len(resolved) - with_image,
        "results":            results,
        "unresolved":         unresolved,
        "rate_limited":       rate_limited,
        "complete":           not rate_limited,
    }


def _fetch_raw_images(stock_item_id: str) -> list[dict]:
    rows = call_linnworks_get(
        "Inventory/GetInventoryItemImages",
        {"inventoryItemId": stock_item_id},
    )
    return rows if isinstance(rows, list) else []


def _image_simple_row(raw: dict, stock_item_id: str) -> dict:
    checksum = raw.get("ChecksumValue") or raw.get("CheckSumValue")
    return {
        "pkRowId":        raw.get("pkRowId"),
        "IsMain":         bool(raw.get("IsMain")),
        "SortOrder":      raw.get("SortOrder"),
        "ChecksumValue":  checksum,
        "RawChecksum":    raw.get("RawChecksum"),
        "StockItemId":    stock_item_id,
        "StockItemIntId": raw.get("StockItemIntId"),
    }


@_tool()
def delete_inventory_item_images(
    sku: str,
    image_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Delete one or more images from an inventory item — the corrective counterpart
    to `add_inventory_item_images`.

    Use this when an image on an item is wrong: a bad colourway, a watermarked
    source, or the same picture added twice. Until now the only fix was editing
    the item by hand in the Linnworks UI.

    IRREVERSIBLE — a deleted image row is gone; re-adding means calling
    `add_inventory_item_images` with the original URL again. Batches of more than
    10 images require confirmed_count=len(image_ids).

    Read-before-write: the item's current images are read first, so the manifest
    shows exactly which image (with its URL and main/sort flags) each id refers
    to. An id that isn't on the item is reported in `unresolved` and skipped —
    it never blocks the ids that are valid. After the write the images are read
    back to confirm each one is gone.

    Args:
        sku: The exact SKU / ItemNumber whose images should be deleted.
        image_ids: Image ids to remove (the `image_id` field returned by
            `get_inventory_item_images` — call that first to pick them).
        confirmed_count: For batches > 10, pass len(image_ids) here.
        dry_run: If True (default), returns the manifest without deleting.
            Set to False to execute.

    Returns:
        A dict with:
          - sku, stock_item_id, dry_run, item_count
          - manifest:        per-id preview of the image that would be deleted
          - unresolved:      ids not present on this item (skipped)
          - deleted:         ids confirmed gone by the read-back (live run only)
          - still_present:   ids the read-back still finds (live run only)
          - remaining_count: images left on the item after the delete (live run)
          - images:          the item's remaining images (live run only)
    """
    if not image_ids:
        raise ValueError("image_ids is empty — nothing to delete.")

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    current = {
        (r.get("pkRowId") or "").lower(): r
        for r in _fetch_raw_images(stock_item_id)
    }

    manifest, unresolved, to_delete = [], [], []
    for img_id in image_ids:
        raw = current.get((img_id or "").strip().lower())
        if raw is None:
            unresolved.append({
                "image_id": img_id,
                "error":    f"Image id '{img_id}' is not on SKU '{sku}' — skipped.",
            })
            continue
        to_delete.append(raw.get("pkRowId"))
        manifest.append({
            **_format_image_row(raw),
            "action": "delete",
        })

    if not to_delete:
        return {
            "sku":            sku,
            "stock_item_id":  stock_item_id,
            "dry_run":        dry_run,
            "item_count":     len(image_ids),
            "manifest":       manifest,
            "unresolved":     unresolved,
            "message":        "None of the given image ids are on this item — nothing to delete.",
        }

    guard = _write_guard("delete_inventory_item_images", to_delete, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "sku": sku, "manifest": manifest, "unresolved": unresolved}

    if dry_run:
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       True,
            "item_count":    len(to_delete),
            "manifest":      manifest,
            "unresolved":    unresolved,
            "message":       (
                f"Dry run — no images deleted. {len(to_delete)} image(s) would be "
                f"removed from '{sku}'. Set dry_run=False to execute."
            ),
        }

    resp = call_linnworks(
        "Inventory/DeleteInventoryItemImageBulk",
        {"request": [{
            "InventoryItemId": stock_item_id,
            "ItemNumber":      sku,
            "ImageIds":        to_delete,
        }]},
    )

    remaining = _fetch_raw_images(stock_item_id)
    remaining_ids = {(r.get("pkRowId") or "").lower() for r in remaining}
    deleted       = [i for i in to_delete if (i or "").lower() not in remaining_ids]
    still_present = [i for i in to_delete if (i or "").lower() in remaining_ids]

    images = [_format_image_row(r) for r in remaining]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":             sku,
        "stock_item_id":   stock_item_id,
        "dry_run":         False,
        "item_count":      len(to_delete),
        "manifest":        manifest,
        "unresolved":      unresolved,
        "deleted":         deleted,
        "still_present":   still_present,
        "remaining_count": len(images),
        "has_main_image":  any(im["is_main"] for im in images),
        "images":          images,
        "result_status":   resp.get("ResultStatus") if isinstance(resp, dict) else None,
    }


@_tool()
def set_inventory_item_image_order(
    sku: str,
    image_ids: list[str] | None = None,
    main_image_id: str | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Reorder an inventory item's images and/or set which one is the main image.

    This matters because the main image is the photo channels show as the
    storefront hero — after `add_inventory_item_images` the ordering isn't
    controllable, so a secondary shot can end up as the listing's front photo.

    ⚠️ THE MAIN IMAGE IS ALWAYS FIRST — Linnworks pins it to sort_order 0 and you
    cannot override that (live-proven 17 Jul 2026). The final order is always:

        [main image] + [your requested order, with the main removed]

    So to change which photo leads, pass `main_image_id` — reordering alone can
    never put a non-main image first. `image_ids` only controls the order of the
    images BEHIND the main one. If the first id you pass isn't the main image,
    the write still succeeds but the main is forced ahead of it; the manifest
    shows the true predicted order and sets `main_forced_first`, so a dry run
    never promises an order Linnworks won't deliver.

    Pass whichever you need (at least one):

      - image_ids:     desired order. May be PARTIAL — any image you don't name
                       keeps its existing relative order and follows the ones you
                       did name.
      - main_image_id: the image to make the hero. Linnworks clears the main flag
                       on the others itself.

    Changing the images does NOT push them to a live listing — follow with
    `refresh_channel_listing` for that, and note the standing warning that a
    green refresh does not by itself prove the storefront photo changed. Read the
    live listing back.

    Args:
        sku: The exact SKU / ItemNumber whose images should be reordered.
        image_ids: Desired image order (may be partial). Omit to leave order alone.
        main_image_id: Image id to make the main/hero image. Omit to leave it alone.
        confirmed_count: For reorders touching > 25 images, pass the number of
            images on the item.
        dry_run: If True (default), returns the manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - sku, stock_item_id, dry_run
          - manifest:          per-image before/after (sort_order, is_main), in
                               the predicted final order
          - main_forced_first: True if the main image was pulled ahead of the
                               first id you asked for
          - unresolved:        ids not present on this item (skipped)
          - images:            the item's images in their new order (live run only)
          - has_main_image:    whether a main image is set (live run only)
    """
    if not image_ids and not main_image_id:
        raise ValueError(
            "Pass image_ids (to reorder), main_image_id (to set the main image), or both."
        )

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    raw_rows = _fetch_raw_images(stock_item_id)
    if not raw_rows:
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       dry_run,
            "message":       f"SKU '{sku}' has no images — nothing to reorder.",
            "images":        [],
        }

    by_id = {(r.get("pkRowId") or "").lower(): r for r in raw_rows}
    unresolved = []

    for label, given in (("image_ids", image_ids or []), ("main_image_id", [main_image_id] if main_image_id else [])):
        for img_id in given:
            if (img_id or "").strip().lower() not in by_id:
                unresolved.append({
                    "image_id": img_id,
                    "field":    label,
                    "error":    f"Image id '{img_id}' is not on SKU '{sku}' — skipped.",
                })

    if main_image_id and any(u["field"] == "main_image_id" for u in unresolved):
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       dry_run,
            "unresolved":    unresolved,
            "error":         (
                f"main_image_id '{main_image_id}' is not an image on SKU '{sku}' — "
                "refusing to write. Call get_inventory_item_images to list valid ids."
            ),
        }

    existing_sorted = sorted(
        raw_rows,
        key=lambda r: (r.get("SortOrder") if r.get("SortOrder") is not None else 0),
    )
    named = [
        by_id[(i or "").strip().lower()]
        for i in (image_ids or [])
        if (i or "").strip().lower() in by_id
    ]
    named_ids = {(r.get("pkRowId") or "").lower() for r in named}
    requested = named + [
        r for r in existing_sorted if (r.get("pkRowId") or "").lower() not in named_ids
    ]

    main_key = (main_image_id or "").strip().lower() or next(
        ((r.get("pkRowId") or "").lower() for r in raw_rows if r.get("IsMain")), ""
    )
    main_row = by_id.get(main_key)
    new_order = (
        [main_row] + [r for r in requested if (r.get("pkRowId") or "").lower() != main_key]
        if main_row else requested
    )
    main_forced_first = bool(
        main_row and requested and (requested[0].get("pkRowId") or "").lower() != main_key
    )

    manifest = []
    for position, raw in enumerate(new_order):
        img_id      = raw.get("pkRowId")
        old_sort    = raw.get("SortOrder")
        old_is_main = bool(raw.get("IsMain"))
        new_is_main = (img_id or "").lower() == main_key
        manifest.append({
            "image_id":       img_id,
            "full_url":       raw.get("FullSource"),
            "old_sort_order": old_sort,
            "new_sort_order": position,
            "old_is_main":    old_is_main,
            "new_is_main":    new_is_main,
            "changed":        old_sort != position or new_is_main != old_is_main,
        })

    main_changing = bool(main_row) and not main_row.get("IsMain")
    order_changing = [m["image_id"] for m in manifest] != [
        r.get("pkRowId") for r in existing_sorted
    ] or any(m["old_sort_order"] != m["new_sort_order"] for m in manifest)

    warning = None
    if main_forced_first:
        warning = (
            f"Image '{requested[0].get('pkRowId')}' was requested first, but Linnworks "
            f"pins the MAIN image to position 0 — it will lead instead. Pass "
            f"main_image_id to change which image is the hero."
        )

    if not order_changing and not main_changing:
        return {
            "sku":               sku,
            "stock_item_id":     stock_item_id,
            "dry_run":           dry_run,
            "manifest":          manifest,
            "unresolved":        unresolved,
            "main_forced_first": main_forced_first,
            "warning":           warning,
            "message":           "Images are already in the requested order — nothing to change.",
        }

    guard = _write_guard(
        "set_inventory_item_image_order", new_order, confirmed_count, dry_run
    )
    if guard is not None:
        return {**guard, "sku": sku, "manifest": manifest, "unresolved": unresolved}

    if dry_run:
        return {
            "sku":               sku,
            "stock_item_id":     stock_item_id,
            "dry_run":           True,
            "manifest":          manifest,
            "unresolved":        unresolved,
            "main_forced_first": main_forced_first,
            "warning":           warning,
            "message":           (
                "Dry run — nothing written. Images would be ordered as shown in the "
                "manifest"
                + (f" and the main image set to '{main_image_id}'" if main_changing else "")
                + ". Set dry_run=False to execute."
            ),
        }

    if main_changing:
        call_linnworks_void(
            "Inventory/SetInventoryItemImageAsMain",
            {"inventoryItemId": stock_item_id, "mainImageId": main_row.get("pkRowId")},
        )

    if order_changing:
        rows = []
        for position, raw in enumerate(new_order):
            row = _image_simple_row(raw, stock_item_id)
            row["SortOrder"] = position
            row["IsMain"] = (raw.get("pkRowId") or "").lower() == main_key
            rows.append(row)
        call_linnworks_void("Inventory/UpdateImages", {"images": rows})

    images = [_format_image_row(r) for r in _fetch_raw_images(stock_item_id)]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":               sku,
        "stock_item_id":     stock_item_id,
        "dry_run":           False,
        "reordered_count":   len(new_order) if order_changing else 0,
        "main_image_set":    main_row.get("pkRowId") if main_changing else None,
        "main_forced_first": main_forced_first,
        "warning":           warning,
        "manifest":          manifest,
        "unresolved":        unresolved,
        "order_matches_plan": [im["image_id"] for im in images] == [m["image_id"] for m in manifest],
        "has_main_image":    any(im["is_main"] for im in images),
        "images":            images,
    }


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(str(title).split()).casefold()


@_tool()
def list_to_shopify(
    skus: list[str],
    configurator: str | None = None,
    default_configurator: str | None = None,
    sub_source: str | None = None,
    allow_duplicate_titles: bool = False,
    known_listed_titles: list[str] | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    List EXISTING Linnworks items on Shopify through the Generic Listing Tool:
    the API version of "select items → apply configurator → create listings".
    Shopify only.

    The configurator is chosen per item: `configurator` (forces one for every
    SKU), else the item's "Shopify Configurator" extended property, else
    `default_configurator`. The name must match a real configurator; a typo, a
    blank property or a name shared by several stores (pass `sub_source`) becomes
    a row in `unresolved` and never sinks the batch.

    ⚠️ A live run creates REAL customer-facing Shopify listings that are not
    trivially undone. Always dry-run first and start small. Creates are
    ASYNCHRONOUS: templates sit at "Creating" for several minutes, so an accepted
    call means queued, not listed.

    ⚠️ Two duplicate checks, both excluding items from the push:
      1. `already_listed`: this item is already live on the target store.
      2. `possible_duplicates`: a DIFFERENT SKU with the same title (normalised,
         exact match) is already live there — the case of one product carried
         under two SKU schemes. The titles compared are the batch's own
         already-listed rows, so pass the whole category, or supply
         `known_listed_titles` when the batch holds only the new SKUs. Set
         allow_duplicate_titles=True to list them anyway.

    Each configurator group is one CreateTemplates call plus one ProcessTemplates
    call. Batches over 25 SKUs stage: confirm with confirmed_count=<N>.

    Args:
        skus: Exact SKUs to list.
        configurator: Force this configurator name for every SKU.
        default_configurator: Fallback for items with no "Shopify Configurator"
            property (used only when `configurator` isn't given).
        sub_source: Shopify store name to scope to; also resolves configurator
            names shared across stores.
        allow_duplicate_titles: List title-matched items anyway (they are still
            reported). Default False.
        known_listed_titles: Extra titles known to be live on the target store.
        confirmed_count: For batches over 25, pass len(skus) after reviewing the
            plan.
        dry_run: Default True: returns the plan and creates nothing.

    Returns:
        A dict with dry_run, item_count, configurator_catalogue_count,
        available_sub_sources;
        plan: per-SKU (sku, stock_item_id, title, configurator, configurator_id,
            channel_id, sub_source, decision);
        groups: items grouped by configurator;
        already_listed; possible_duplicates (duplicate_of_skus, matched_on,
            listed_anyway);
        unresolved: per-SKU reasons;
        results: per-group created template ids and process status (live run
            only).
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("configurator", configurator or "")
    _check_injection("default_configurator", default_configurator or "")
    _check_injection("sub_source", sub_source or "")

    catalogue = _fetch_shopify_configurators()
    available_sub_sources = sorted(
        {c["sub_source"] for c in catalogue if c.get("sub_source")}
    )

    scope = catalogue
    if sub_source:
        scope = [c for c in catalogue if _norm_conf_name(c.get("sub_source")) == _norm_conf_name(sub_source)]
        if not scope:
            return {
                "error": (
                    f"sub_source '{sub_source}' is not a Shopify store in this account. "
                    f"Available: {available_sub_sources}"
                ),
                "available_sub_sources": available_sub_sources,
            }

    by_name: dict[str, list[dict]] = {}
    for c in scope:
        by_name.setdefault(_norm_conf_name(c.get("name")), []).append(c)

    sku_cache: dict[str, str] = {}
    plan: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []

    for raw_sku in skus:
        sku = (raw_sku or "").strip()
        if not sku:
            unresolved.append({"sku": raw_sku, "error": "empty SKU"})
            continue

        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "error": f"not found: {exc}"})
            continue
        stock_item_id = item.get("StockItemId")
        if not stock_item_id:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        sku_cache[sku] = stock_item_id
        title = item.get("ItemTitle")

        decision: str
        conf_name: str | None
        if configurator:
            conf_name, decision = configurator, "override"
        else:
            prop_value = None
            try:
                props = call_linnworks(
                    "Inventory/GetInventoryItemExtendedProperties",
                    {"inventoryItemId": stock_item_id},
                )
                for p in (props if isinstance(props, list) else []):
                    if _norm_conf_name(p.get("ProperyName")) == _norm_conf_name(SHOPIFY_CONFIGURATOR_PROPERTY):
                        val = (p.get("PropertyValue") or "").strip()
                        if val:
                            prop_value = val
                        break
            except RuntimeError:
                prop_value = None

            if prop_value:
                conf_name, decision = prop_value, "extended_property"
            elif default_configurator:
                conf_name, decision = default_configurator, "default"
            else:
                unresolved.append({
                    "sku": sku, "stock_item_id": stock_item_id, "title": title,
                    "error": (
                        f"no configurator: item has no '{SHOPIFY_CONFIGURATOR_PROPERTY}' "
                        "extended property and no default_configurator was given"
                    ),
                })
                continue

        matches = by_name.get(_norm_conf_name(conf_name), [])
        if not matches:
            unresolved.append({
                "sku": sku, "stock_item_id": stock_item_id, "title": title,
                "configurator": conf_name, "decision": decision,
                "error": (
                    f"configurator '{conf_name}' not found in the Shopify catalogue"
                    + (f" for store '{sub_source}'" if sub_source else "")
                    + " — check spelling/casing against available configurators"
                ),
            })
            continue
        if len(matches) > 1:
            unresolved.append({
                "sku": sku, "stock_item_id": stock_item_id, "title": title,
                "configurator": conf_name, "decision": decision,
                "error": (
                    f"ambiguous: '{conf_name}' exists on "
                    f"{len(matches)} stores ({[m['sub_source'] for m in matches]}) — "
                    "pass sub_source to disambiguate"
                ),
            })
            continue

        chosen = matches[0]
        plan.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "title":           title,
            "configurator":    chosen.get("name"),
            "configurator_id": chosen.get("id"),
            "channel_id":      chosen.get("channel_id"),
            "sub_source":      chosen.get("sub_source"),
            "decision":        decision,
        })

    already_listed: list[dict] = []
    dedupe_warning: str | None = None
    if plan:
        try:
            channel_map = _fetch_channel_skus_for_ids([r["stock_item_id"] for r in plan])
            still_to_list: list[dict] = []
            for row in plan:
                existing = [
                    _format_channel_sku_row(cr)
                    for cr in channel_map.get((row["stock_item_id"] or "").lower(), [])
                    if _norm_conf_name(cr.get("Source")) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME)
                    and _norm_conf_name(cr.get("SubSource")) == _norm_conf_name(row["sub_source"])
                ]
                if existing:
                    already_listed.append({**row, "existing_listings": existing})
                else:
                    still_to_list.append(row)
            plan = still_to_list
        except RuntimeError as exc:
            dedupe_warning = (
                f"could not check existing channel listings ({exc}); proceeding "
                "WITHOUT dedupe — verify nothing is double-listed"
            )

    possible_duplicates: list[dict] = []
    if plan:
        live_titles: dict[str, list[str]] = {}
        for row in already_listed:
            key = _norm_title(row.get("title"))
            if key:
                live_titles.setdefault(key, []).append(row.get("sku"))
        for t in (known_listed_titles or []):
            key = _norm_title(t)
            if key:
                live_titles.setdefault(key, [])

        if live_titles:
            keep: list[dict] = []
            for row in plan:
                key = _norm_title(row.get("title"))
                match = live_titles.get(key) if key else None
                if match is None:
                    keep.append(row)
                    continue
                dup = {
                    **row,
                    "duplicate_of_skus": [s for s in match if s],
                    "matched_on": "normalised ItemTitle",
                    "reason": (
                        "a product with an identical title is already live on "
                        f"'{row['sub_source']}' under a different SKU — listing this would "
                        "create a second Shopify product for the same physical item"
                    ),
                }
                if allow_duplicate_titles:
                    dup["listed_anyway"] = True
                    keep.append(row)
                else:
                    dup["listed_anyway"] = False
                possible_duplicates.append(dup)
            if not allow_duplicate_titles:
                plan = keep

    groups: dict[int, dict] = {}
    for row in plan:
        cid = row["configurator_id"]
        g = groups.setdefault(cid, {
            "configurator":    row["configurator"],
            "configurator_id": cid,
            "channel_id":      row["channel_id"],
            "sub_source":      row["sub_source"],
            "skus":            [],
            "inventory_item_ids": [],
        })
        g["skus"].append(row["sku"])
        g["inventory_item_ids"].append(row["stock_item_id"])
    group_list = list(groups.values())

    base_out = {
        "item_count":                    len(skus),
        "configurator_catalogue_count":  len(catalogue),
        "available_sub_sources":         available_sub_sources,
        "plan":                          plan,
        "groups":                        [
            {k: v for k, v in g.items() if k != "inventory_item_ids"}
            for g in group_list
        ],
        "already_listed":                already_listed,
        "possible_duplicates":           possible_duplicates,
        "unresolved":                    unresolved,
        "rate_limited":                  rate_limited,
        "complete":                      not rate_limited,
    }
    if dedupe_warning:
        base_out["dedupe_warning"] = dedupe_warning
    if possible_duplicates and not allow_duplicate_titles:
        base_out["duplicate_warning"] = (
            f"{len(possible_duplicates)} SKU(s) share a title with a product already live on "
            "the target store under a DIFFERENT SKU and were EXCLUDED — see possible_duplicates. "
            "This is the SKU-migration case that created 177 duplicate Shopify products on "
            "6 Aug 2026. Pass allow_duplicate_titles=True only if you are sure they are "
            "genuinely different products."
        )

    guard = _write_guard("list_to_shopify", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing listed. {len(plan)} SKU(s) across "
                f"{len(group_list)} configurator group(s) would be listed to Shopify; "
                f"{len(already_listed)} SKU(s) already live on their target store and "
                f"skipped (see already_listed); "
                f"{len(possible_duplicates)} SKU(s) share a title with a product already live "
                f"under a different SKU "
                f"({'listed anyway — allow_duplicate_titles=True' if allow_duplicate_titles else 'EXCLUDED'}, "
                "see possible_duplicates); "
                f"{len(unresolved)} SKU(s) could not be resolved (see unresolved). "
                "Review the plan, then set dry_run=False to create and push the "
                "listings. A live run creates real customer-facing Shopify listings."
            ),
        }

    if not group_list:
        msg = "No SKUs resolved to a configurator; nothing was listed."
        if already_listed:
            msg = (
                f"Nothing listed — all resolved SKU(s) were already live on their "
                f"target store ({len(already_listed)} skipped, see already_listed)."
            )
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": msg,
        }

    results: list[dict] = []
    for g in group_list:
        ids = g["inventory_item_ids"]
        result = {
            "configurator":    g["configurator"],
            "configurator_id": g["configurator_id"],
            "channel_id":      g["channel_id"],
            "sub_source":      g["sub_source"],
            "sku_count":       len(ids),
            "created_template_ids": [],
            "processed":       False,
        }
        try:
            create_resp = call_linnworks(
                "GenericListings/CreateTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "Parameters": {
                        "SelectedRegions":  [],
                        "Token":            _ZERO_GUID,
                        "InventoryItemIds": ids,
                        "ChannelId":        g["channel_id"],
                    },
                    "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(ids), 1)},
                    "ConfiguratorId": g["configurator_id"],
                }},
            )
        except RuntimeError as exc:
            result["error"] = f"CreateTemplates failed: {exc}"
            results.append(result)
            continue

        created_ids = []
        if isinstance(create_resp, dict):
            created_ids = create_resp.get("AllCreatedIds") or []
        result["created_template_ids"] = created_ids

        if not created_ids:
            result["error"] = (
                "CreateTemplates returned no template ids (AllCreatedIds empty) — "
                "nothing to process. Inspect TemplatesInfo in the Linnworks UI."
            )
            results.append(result)
            continue

        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "TemplateRequests": [
                        {"TemplateId": tid, "Action": "Create"} for tid in created_ids
                    ],
                    "ClientContext": {"Activity": "list_to_shopify", "Source": "linnworks-plugin"},
                }},
            )
            result["processed"] = True
        except RuntimeError as exc:
            result["error"] = (
                f"templates created (ids {created_ids}) but ProcessTemplates failed: {exc}"
            )
        results.append(result)

    listed = sum(1 for r in results if r.get("processed"))
    return {
        "dry_run":    False,
        **base_out,
        "results":    results,
        "message": (
            f"{listed}/{len(results)} configurator group(s) listed and pushed to "
            "Shopify. Verify the new listings in the Linnworks GLT / your Shopify "
            "admin — there is no clean API read-back for newly created listings. "
            "Any per-group error is surfaced in results[].error."
        ),
    }


_GLT_PROCESS_ACTIONS = {
    "Create", "Update", "Relist", "Delete", "Revise", "ChannelSpecific", "NotAllowed",
}
_GLT_REFRESH_ACTIONS = {"Update", "Revise", "Relist", "ChannelSpecific"}


_GLT_UNDETECTABLE_FIELDS = [
    "image content/URL (the template exposes only a COUNT)",
    "description body (exposed only as \"Filled\")",
    "attributes (exposed only as a count)",
    "metafields (exposed only as a count)",
]


def _glt_snapshot_age_days(last_modified: str | None) -> int | None:
    if not last_modified:
        return None
    raw = str(last_modified).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _effective_channel_value(rows, sub_source: str, key: str,
                             channel_source: str = GLT_SHOPIFY_CHANNEL_NAME, fallback=None):
    for r in rows if isinstance(rows, list) else []:
        if (_norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
                and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)):
            val = r.get(key)
            if val is not None:
                return val, "channel_override"
    return fallback, "base"


def _refresh_staleness_message(check_staleness: bool, stale_rows: list, no_diff_rows: list,
                               unchecked_rows: list, live: bool = False) -> str:
    if not check_staleness:
        return ("Staleness NOT checked (check_staleness=False) — the snapshot may disagree "
                "with the item. ")
    bits = []
    if stale_rows:
        verb = "pushed a STALE snapshot" if live else "would push a STALE snapshot"
        names = ", ".join(
            f"{r['sku']} ({'/'.join(r['staleness']['stale_fields'])})" for r in stale_rows[:5])
        more = f" +{len(stale_rows) - 5} more" if len(stale_rows) > 5 else ""
        bits.append(f"⚠️ {len(stale_rows)} template(s) {verb}: {names}{more} — the stored value "
                    "overwrites the current one; rebuild the template in the Linnworks GLT UI.")
    if no_diff_rows:
        bits.append(f"{len(no_diff_rows)} template(s) show NO DETECTABLE difference — which may "
                    "mean a silent no-op, NOT that the push worked: image URLs, description, "
                    "attributes and metafields are invisible to this check.")
    if unchecked_rows:
        bits.append(f"{len(unchecked_rows)} template(s) could NOT be checked (read failed) — "
                    "treat their freshness as unknown.")
    return (" ".join(bits) + " ") if bits else ""


def _glt_template_staleness(template: dict, stock_item_id: str, base_title: str | None,
                            base_price, sub_source: str,
                            channel_source: str = GLT_SHOPIFY_CHANNEL_NAME) -> dict:
    info = template.get("Info") if isinstance(template.get("Info"), dict) else {}
    last_mod = _glt_field(info, "LastModificationTime")
    out: dict = {
        "checked": True,
        "template_last_modified": last_mod,
        "snapshot_age_days": _glt_snapshot_age_days(last_mod),
        "compared": {},
        "stale_fields": [],
        "skipped_comparisons": [],
        "comparable_fields_match": None,
        "undetectable_fields": list(_GLT_UNDETECTABLE_FIELDS),
    }

    try:
        title_rows = call_linnworks_get(
            "Inventory/GetInventoryItemTitles", {"inventoryItemId": stock_item_id})
        price_rows = call_linnworks_get(
            "Inventory/GetInventoryItemPrices", {"inventoryItemId": stock_item_id})
        images = call_linnworks_get(
            "Inventory/GetInventoryItemImages", {"inventoryItemId": stock_item_id})
    except (RateLimitError, RuntimeError) as exc:
        out["checked"] = False
        out["error"] = f"could not read current item values: {exc}"
        out["warning"] = (
            "Staleness UNKNOWN — the current-value read failed, so this is not a "
            "statement that the snapshot is fresh."
        )
        return out

    tpl_title = _glt_field(info, "Title")
    cur_title, title_src = _effective_channel_value(
        title_rows, sub_source, "Title", channel_source, fallback=base_title)
    if tpl_title is not None and cur_title is not None:
        match = str(tpl_title).strip() == str(cur_title).strip()
        out["compared"]["title"] = {
            "template": tpl_title, "item": cur_title,
            "item_value_from": title_src, "match": match,
        }
        if not match:
            out["stale_fields"].append("title")

    if template.get("IsVariation"):
        out["skipped_comparisons"].append(
            "price (variation template — the snapshot reports 0.0 because prices "
            "live per-variant, so a comparison would be a false positive)"
        )
        out["undetectable_fields"].append("per-variant prices on a variation template")
    else:
        tpl_price = _glt_field(info, "Price")
        cur_price, price_src = _effective_channel_value(
            price_rows, sub_source, "Price", channel_source, fallback=base_price)
        if tpl_price is not None and cur_price is not None:
            try:
                match = abs(float(tpl_price) - float(cur_price)) < 0.005
            except (TypeError, ValueError):
                match = str(tpl_price) == str(cur_price)
            out["compared"]["price"] = {
                "template": tpl_price, "item": cur_price,
                "item_value_from": price_src, "match": match,
            }
            if not match:
                out["stale_fields"].append("price")

    tpl_images = _glt_field(info, "Images")
    cur_count = len(images) if isinstance(images, list) else None
    if tpl_images is not None and cur_count is not None:
        try:
            tpl_count = int(str(tpl_images).strip())
        except (TypeError, ValueError):
            tpl_count = None
        if tpl_count is not None:
            match = tpl_count == cur_count
            out["compared"]["image_count"] = {
                "template": tpl_count, "item": cur_count, "match": match,
                "note": ("COUNT ONLY — the template does not expose image URLs, so an "
                         "equal count does NOT mean the same images (this is exactly "
                         "how a silent no-op once went undetected)"),
            }
            if not match:
                out["stale_fields"].append("image_count")

    out["comparable_fields_match"] = not out["stale_fields"]

    age = out["snapshot_age_days"]
    age_txt = f"built {age} day(s) ago" if age is not None else "build time unknown"
    if out["stale_fields"]:
        out["warning"] = (
            f"STALE — the snapshot ({age_txt}) disagrees with the item on "
            f"{', '.join(out['stale_fields'])}. Pushing it will send the STORED value "
            "and overwrite the current one on the live listing. Rebuild the template "
            "by opening the listing in the Linnworks GLT UI first."
        )
    else:
        out["warning"] = (
            f"No DETECTABLE difference on the comparable fields, but the snapshot was "
            f"{age_txt} and this is NOT a guarantee the push will change anything: "
            "image URLs, description body, attributes and metafields are invisible "
            "here. An earlier push was a silent no-op with every comparable field matching. "
            "Read the live listing back after pushing."
        )
    return out


@_tool()
def refresh_channel_listing(
    skus: list[str],
    sub_source: str = DEFAULT_SHOPIFY_SUB_SOURCE,
    channel: str = "Shopify",
    action: str | None = None,
    check_staleness: bool = True,
    check_listing_exists: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Re-push EXISTING listings on a GLT channel so edited item data (title, price,
    description, extended properties) reaches the live listing. Channels: Shopify
    (default), Amazon, TikTok, Magento, Walmart. A non-GLT channel (eBay, Etsy,
    Mirakl, CDiscount) raises a ValueError naming the supported ones. It never
    creates a listing: a SKU not already live on `sub_source` goes to `unresolved`
    (use list_to_shopify to create one).

    ⚠️ Proven on Shopify only. On Amazon the push has been fired live and changed
    nothing observable; on TikTok it has never been tried. Every row carries
    `revise_proven` and `push_observed_state`, and every message warns about an
    unproven channel.

    ⚠️ STALE SNAPSHOT: the push sends the template's STORED field snapshot, not the
    item's current data. An old template overwrites live values with old ones (one
    once reverted a live selling price). "Revise" and "Update" behave the same. No
    API refreshes a variation template's fields; opening the listing in the
    Linnworks GLT UI rebuilds them. Only live-run templates you know are fresh.

    ⚠️ SILENT NO-OP: `processed: true` means the push was ACCEPTED, never that
    anything changed. A push can leave the channel untouched (e.g. when the stored
    image URL no longer exists). Read the listing back after every live run, using
    listing data (get_channel_listings or the channel's reporting), not the
    channel's detail page, which on Amazon can lag a day.

    Per SKU: resolve it → confirm a channel-SKU mapping on `sub_source` → open the
    item's existing template(s) with OpenTemplatesByInventory (this never creates
    one) → on a live run, ProcessTemplates per template. An item can have several
    templates on one channel (Amazon merchant + ".FBA"); all are planned, one row
    each, because reviving only one leaves the other stale.

    Variation groups: on Shopify the PARENT holds the template and the children hold
    the channel-SKU rows. A child with no template falls back to its parent's
    (`via_variation_parent`); children of one group dedupe to ONE push that revises
    every variant (`covers_skus`); a parent counts as listed when any child is
    mapped (`listed_via_children`). This is how a Shopify compare-at price (mapped
    from the `special_price` extended property) reaches variant listings: update
    `special_price` on the children, then refresh. On Amazon one family (bushings)
    had a template per child, TikTok's shape — one observation, not a rule — so
    treat any Amazon variation result as unverified.

    Action: action=None uses the template's NextSuggestedAction when GLT allows it,
    else "Revise"; pass action="Revise"/"Update"/… to force one. Locked templates,
    or ones with no allowed action, go to `unresolved`. NextSuggestedAction is NOT a
    sign that a change is pending (it reads "Update" on almost every listed Shopify
    template); use `staleness` for that.

    Batches over 25 SKUs stage: the plan comes back first, and you confirm with
    confirmed_count=<N>.

    Args:
        skus: Exact SKUs whose listings to refresh.
        sub_source: Store or account that the listing check and templates are
            scoped to. Shopify: the store name. Amazon: the account, or a regional
            sub-source ("<account> - Germany"), which resolves to the account
            (`sub_source_resolution: "account-prefix"`). TikTok: the shop name.
        channel: "Shopify" (default), "Amazon", "TikTok", "Magento" or "Walmart".
        action: Optional GLT action override. Default None = auto (see above).
        check_staleness: Default True. Compares each template's stored snapshot
            with the item's current title, price and image count (3 extra GETs per
            template; title and price use THIS channel's override). It is PARTIAL:
            image URLs, description, attributes and metafields can't be compared,
            so `comparable_fields_match: true` means "no difference visible", not
            "this push will change something". Price is skipped on variation
            templates. False skips the reads and says nothing about freshness.
        check_listing_exists: Default True, Shopify only. Checks each template's
            stored listing id still exists on Shopify (needs Shopify Admin
            credentials; without them it reports the check as not run, never as
            passed). A template pointing at a deleted listing goes to `unresolved`
            with blocked_reason "dangling_listing" and is NOT pushed, because one
            dangling template makes Linnworks fail every template batched with it.
        confirmed_count: For batches over 25, pass len(skus) after reviewing the
            plan.
        dry_run: Default True: returns the plan and pushes nothing.

    Returns:
        A dict with dry_run, item_count, target_channel, target_source,
        target_sub_source, target_channel_id, sub_source_resolution, revise_proven,
        available_sub_sources;
        plan: one row per template (sku, covers_skus, stock_item_id, title,
            channel, sub_source, template_id, configurator_id, active_listing_id,
            status, action, next_suggested_action, is_allowed_to_revise,
            templates_on_item, revise_proven, via_variation_parent /
            listed_via_children, listing_exists, listing_check, and `staleness`:
            template_last_modified, snapshot_age_days, compared, stale_fields,
            comparable_fields_match, skipped_comparisons, undetectable_fields,
            warning);
        stale_plan_count, no_detectable_change_count, staleness_unchecked_count,
        staleness_note; listing_existence_checked, dangling_listing_count,
        dangling_listings;
        unresolved: per-SKU rows with the reason;
        results: per-template push outcome (live run only).
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")
    _check_injection("channel", channel or "")
    _check_injection("action", action or "")

    if action is not None and action not in _GLT_PROCESS_ACTIONS:
        raise ValueError(
            f"action '{action}' is not a valid GLT action. Valid: {sorted(_GLT_PROCESS_ACTIONS)}"
        )

    target = _resolve_glt_target(channel, sub_source)
    ch = target["channel"]
    channel_source = ch["source"]
    available_sub_sources = target["available_sub_sources"]
    if not target["ok"]:
        return {"error": target["error"], "available_sub_sources": available_sub_sources}
    target_channel_id = target["channel_id"]

    resolved: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []
    _variation_cache: dict[str, dict | None] = {}

    def _parent_of_child(child_sku: str, child_sid: str) -> dict | None:
        key = child_sku.strip().lower()
        if key in _variation_cache:
            return _variation_cache[key]
        rel = _resolve_variation(child_sku, child_sid)
        entry = None
        if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
            entry = {
                "parent_sku": rel.get("parent_sku"),
                "parent_sid": rel.get("parent_stock_item_id"),
            }
            for sib in rel.get("siblings", []):
                if sib.get("sku"):
                    _variation_cache[sib["sku"].strip().lower()] = entry
        _variation_cache[key] = entry
        return entry

    def _rows_on_store(rows: list) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        title = item.get("ItemTitle")
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "error": f"could not read channel listings: {exc}",
            })
            continue
        if _rows_on_store(rows):
            resolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "base_price": item.get("RetailPrice"),
            })
            continue

        rel = _resolve_variation(sku, sid)
        if rel.get("role") == "parent" and rel.get("children"):
            child_map = _fetch_channel_skus_for_ids(
                [c["stock_item_id"] for c in rel["children"] if c.get("stock_item_id")]
            )
            listed_children = [
                c["sku"] for c in rel["children"]
                if _rows_on_store(child_map.get((c.get("stock_item_id") or "").lower(), []))
            ]
            if listed_children:
                resolved.append({
                    "sku": sku, "stock_item_id": sid, "title": title,
                    "base_price": item.get("RetailPrice"),
                    "listed_via_children": listed_children,
                })
                continue

        unresolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "error": (
                f"not listed on {ch['channel_type']} '{sub_source}' — "
                "use list_to_shopify to create it first"
            ),
        })

    templates_by_sid: dict[str, list[dict]] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    child_fallback: dict[str, dict] = {}
    parent_sids_to_open: list[str] = []
    for r in resolved:
        if templates_by_sid.get(r["stock_item_id"].lower()) or "listed_via_children" in r:
            continue
        entry = _parent_of_child(r["sku"], r["stock_item_id"])
        if entry and entry.get("parent_sid"):
            child_fallback[r["sku"].strip().lower()] = entry
            if entry["parent_sid"].lower() not in templates_by_sid:
                parent_sids_to_open.append(entry["parent_sid"])
    parent_sids_to_open = list(dict.fromkeys(parent_sids_to_open))
    for i in range(0, len(parent_sids_to_open), 200):
        chunk = parent_sids_to_open[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    plan: list[dict] = []
    plan_by_template: dict = {}
    base_info_by_sid: dict[str, dict] = {
        r["stock_item_id"].lower(): {"title": r.get("title"), "base_price": r.get("base_price")}
        for r in resolved
    }
    for r in resolved:
        templates = templates_by_sid.get(r["stock_item_id"].lower()) or []
        via_parent = None
        if not templates:
            via_parent = child_fallback.get(r["sku"].strip().lower())
            if via_parent and via_parent.get("parent_sid"):
                templates = templates_by_sid.get(via_parent["parent_sid"].lower()) or []
        if not templates:
            unresolved.append({
                **r,
                "error": (
                    "listed on the channel but no GLT template could be opened "
                    "for it (checked the item and its variation parent)"
                ),
            })
            continue

        for t in templates:
            existing = plan_by_template.get(t.get("Id"))
            if existing is not None:
                if r["sku"] not in existing["covers_skus"]:
                    existing["covers_skus"].append(r["sku"])
                continue

            if t.get("IsLocked"):
                unresolved.append({
                    **r, "template_id": t.get("Id"),
                    "error": "GLT template is locked — cannot revise right now",
                })
                continue

            next_action  = t.get("NextSuggestedAction")
            next_allowed = bool(t.get("IsNextSuggestedActionAllowed"))
            can_revise   = bool(t.get("IsAllowedToRevise"))

            if action is not None:
                chosen_action = action
            elif next_allowed and next_action in _GLT_REFRESH_ACTIONS:
                chosen_action = next_action
            elif can_revise:
                chosen_action = "Revise"
            else:
                unresolved.append({
                    **r, "template_id": t.get("Id"), "next_suggested_action": next_action,
                    "error": "GLT does not allow a revise/update on this template (no allowed push action)",
                })
                continue

            info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
            row = {
                "sku":                   via_parent["parent_sku"] if via_parent else r["sku"],
                "stock_item_id":         via_parent["parent_sid"] if via_parent else r["stock_item_id"],
                "title":                 r["title"],
                "channel":               ch["channel_type"],
                "sub_source":            sub_source,
                "templates_on_item":     len(templates),
                "revise_proven":         ch["revise_proven"],
                "push_observed_state":   ch["push_observed_state"],
                "template_id":           t.get("Id"),
                "configurator_id":       t.get("ConfiguratorId"),
                "active_listing_id":     _glt_field(info, "ActiveListingId"),
                "status":                _glt_field(info, "Status"),
                "action":                chosen_action,
                "next_suggested_action": next_action,
                "is_allowed_to_revise":  can_revise,
                "covers_skus":           [r["sku"]],
            }
            if via_parent:
                row["via_variation_parent"] = True
            if "listed_via_children" in r:
                row["listed_via_children"] = r["listed_via_children"]

            if check_staleness:
                base_title, base_price = r.get("title"), r.get("base_price")
                if via_parent:
                    cached = base_info_by_sid.get(row["stock_item_id"].lower())
                    if cached is None:
                        try:
                            pitem = call_linnworks(
                                "Inventory/GetInventoryItem", {"sku": row["sku"]})
                            cached = {"title": pitem.get("ItemTitle"),
                                      "base_price": pitem.get("RetailPrice")}
                        except (RateLimitError, RuntimeError):
                            cached = {}
                        base_info_by_sid[row["stock_item_id"].lower()] = cached
                    base_title = cached.get("title")
                    base_price = cached.get("base_price")
                row["staleness"] = _glt_template_staleness(
                    t, row["stock_item_id"], base_title, base_price, sub_source,
                    channel_source)

            plan.append(row)
            plan_by_template[t.get("Id")] = row

    listing_check = {"checked": False, "reason": None, "by_row": {}}
    dangling_rows: list[dict] = []
    if check_listing_exists and plan:
        listing_check = _glt_listing_existence(plan, sub_source, channel_source)
        if listing_check["checked"]:
            still_planned = []
            for row in plan:
                gid = _shopify_product_gid(row.get("active_listing_id"))
                if not gid:
                    row["listing_exists"] = None
                    row["listing_check"] = "no_listing_id"
                    still_planned.append(row)
                    continue
                probe = listing_check["by_row"].get(gid, {})
                exists = probe.get("exists")
                row["listing_exists"] = exists
                if exists is True:
                    row["listing_check"] = "exists"
                    still_planned.append(row)
                elif exists is None:
                    row["listing_check"] = "unknown"
                    still_planned.append(row)
                else:
                    row["listing_check"] = "gone"
                    dangling_rows.append(row)
                    unresolved.append({
                        "sku": row["sku"],
                        "stock_item_id": row["stock_item_id"],
                        "title": row.get("title"),
                        "template_id": row["template_id"],
                        "active_listing_id": row.get("active_listing_id"),
                        "blocked_reason": "dangling_listing",
                        "error": (
                            f"DANGLING TEMPLATE — the template's stored "
                            f"ActiveListingId ({row.get('active_listing_id')}) is not a "
                            f"product that exists on Shopify '{sub_source}' any more. "
                            "It was almost certainly deleted on the channel (a "
                            "duplicate cleanup does exactly this) while the template "
                            "kept pointing at it. The channel-SKU mapping can look "
                            "perfectly healthy at the same time, so this is invisible "
                            "to get_channel_listings. Pushing it CANNOT work and will "
                            "fail every other template pushed alongside it with "
                            "\"Error reading JObject from JsonReader ... Path "
                            "'data.nodes[N]'\". Fix: rebuild or remove this template in "
                            "the Linnworks GLT UI (open the listing so the template "
                            "re-points at the surviving product), then re-run."
                        ),
                    })
            plan = still_planned
        else:
            for row in plan:
                row["listing_exists"] = None
                row["listing_check"] = (listing_check["reason"] or "unchecked").split(" — ")[0]

    stale_rows       = [r for r in plan if r.get("staleness", {}).get("stale_fields")]
    unchecked_rows   = [r for r in plan if "staleness" in r and not r["staleness"].get("checked")]
    no_diff_rows     = [r for r in plan
                        if r.get("staleness", {}).get("comparable_fields_match") is True]

    base_out = {
        "item_count":            len(skus),
        "target_channel":        ch["channel_type"],
        "target_source":         channel_source,
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "sub_source_resolution": target["resolution"],
        "revise_proven":         ch["revise_proven"],
        "push_observed_state":   ch["push_observed_state"],
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
        "rate_limited":          rate_limited,
        "complete":              not rate_limited,
    }
    if check_listing_exists:
        base_out["listing_existence_checked"] = bool(listing_check["checked"])
        base_out["dangling_listing_count"] = len(dangling_rows)
        if not listing_check["checked"]:
            base_out["listing_existence_note"] = (
                f"Listing-existence NOT verified ({listing_check['reason']}). A template "
                "whose stored ActiveListingId points at a listing that has been deleted on "
                "the channel is INVISIBLE to both the channel-SKU table and the staleness "
                "check, and pushing it fails every other template in the same batch"
                "."
            )
        elif dangling_rows:
            base_out["dangling_listings"] = [
                {"sku": r["sku"], "template_id": r["template_id"],
                 "active_listing_id": r.get("active_listing_id")}
                for r in dangling_rows
            ]
    else:
        base_out["listing_existence_checked"] = False

    if check_staleness:
        base_out["staleness_checked"] = True
        base_out["stale_plan_count"] = len(stale_rows)
        base_out["no_detectable_change_count"] = len(no_diff_rows)
        base_out["staleness_unchecked_count"] = len(unchecked_rows)
        base_out["staleness_note"] = (
            "Staleness compares the template's STORED snapshot against the item's "
            "current values. Only title, price (non-variation) and image COUNT are "
            "comparable — image URLs, description body, attributes and metafields are "
            "not exposed, so 'no detectable change' is NOT a promise that the push "
            "will alter the listing (an earlier push was a silent no-op with every "
            "comparable field matching)."
        )
    else:
        base_out["staleness_checked"] = False

    guard = _write_guard("refresh_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if ch["revise_proven"]:
        unproven_note = ""
    elif ch["push_observed_state"] == PUSH_ACCEPTED_NOT_PROCESSED:
        unproven_note = (
            f" ⚠️  {_push_observation_reason(ch)} This channel is TRIED AND SHOWN "
            f"INEFFECTIVE, not merely untried (proven: {_proven_revise_channels()}). "
            "Do not trust a bulk push here without independently reading the listing "
            "data back afterwards."
        )
    else:
        unproven_note = (
            f" ⚠️  ProcessTemplates Revise/Update has NEVER been attempted live on "
            f"{ch['channel_type']} (proven: {_proven_revise_channels()}) — prove it on ONE "
            "low-risk listing (check the template's freshness first, then read the listing "
            "DATA back, not the detail page) before trusting a bulk run on this channel."
        )

    dangling_note = ""
    if dangling_rows:
        names = ", ".join(f"{r['sku']} (tpl {r['template_id']})" for r in dangling_rows[:5])
        more = "" if len(dangling_rows) <= 5 else f" (+{len(dangling_rows) - 5} more)"
        dangling_note = (
            f"⛔ {len(dangling_rows)} DANGLING template(s) EXCLUDED from the push — their "
            f"stored ActiveListingId no longer exists on the channel: {names}{more}. These "
            "cannot be revised, and pushing one fails every healthy template batched with it"
            ". Rebuild or remove them in the Linnworks GLT UI. "
        )
    elif check_listing_exists and not listing_check["checked"]:
        dangling_note = f"⚠️ {base_out.get('listing_existence_note', '')} "

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing pushed. {len(plan)} {ch['channel_type']} listing(s) on "
                f"'{sub_source}' would be revised; {len(unresolved)} SKU(s) could not be revised "
                "(see unresolved). " + dangling_note
                + _refresh_staleness_message(check_staleness, stale_rows, no_diff_rows,
                                             unchecked_rows) +
                "Review the plan, then set dry_run=False to push the revisions. A live run changes "
                f"real customer-facing listings.{unproven_note}"
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": (
                f"Nothing to revise — no SKU resolved to an existing, revisable {ch['channel_type']} "
                "template. " + dangling_note
            ),
        }

    results: list[dict] = []
    pushed = 0
    for row in plan:
        res = {
            "sku":         row["sku"],
            "covers_skus": list(row.get("covers_skus") or [row["sku"]]),
            "channel":     ch["channel_type"],
            "sub_source":  sub_source,
            "template_id": row["template_id"],
            "action":      row["action"],
            "processed":   False,
        }
        st = row.get("staleness")
        if st is not None:
            res["stale_fields"] = st.get("stale_fields")
            res["comparable_fields_match"] = st.get("comparable_fields_match")
            res["snapshot_age_days"] = st.get("snapshot_age_days")
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
                    "TemplateRequests": [
                        {"TemplateId": row["template_id"], "Action": row["action"]}
                    ],
                    "ClientContext": {"Activity": "refresh_channel_listing", "Source": "linnworks-plugin"},
                }},
            )
            res["processed"] = True
            pushed += 1
        except RuntimeError as exc:
            res["error"] = f"ProcessTemplates ({row['action']}) failed: {exc}"
        results.append(res)

    readback_note = (
        "ProcessTemplates returns no body, so `processed: true` means the push was ACCEPTED, "
        "never that the listing changed — this is NOT proof of a change. READ THE LISTING DATA "
        "BACK NOW (e.g. get_channel_listings), not the channel's detail/storefront page — "
    )
    if ch["channel_type"] == "Amazon":
        readback_note += "Amazon detail pages in particular can lag the catalogue by up to a day. "
    else:
        readback_note += "a green result plus an unchanged detail page proves nothing either way. "

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "message": (
            f"{pushed}/{len(plan)} {ch['channel_type']} listing(s) on '{sub_source}' revised and "
            "pushed. " + dangling_note
            + _refresh_staleness_message(check_staleness, stale_rows, no_diff_rows,
                                         unchecked_rows, live=True) +
            readback_note +
            "The push sends the template's STORED field snapshot, which can be stale and overwrite "
            "current prices/content (live-proven on Shopify 14 Jul 2026) or change nothing at all "
            f". Per-item errors are in results[].error.{unproven_note}"
        ),
    }


@_tool()
def unpublish_channel_listing(
    skus: list[str],
    sub_source: str = DEFAULT_SHOPIFY_SUB_SOURCE,
    channel: str = "Shopify",
    allow_variation_parent_takedown: bool = True,
    also_retiring_skus: list[str] | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take EXISTING channel listings down so they stop selling, using the GLT
    "Delete" action on the item's existing template(s). Channels: Shopify
    (default), Amazon, TikTok, Magento, Walmart. Use it to retire a duplicate or
    orphaned listing, or as the take-down step of a dead-product cleanup (delist →
    wait for channel sync → archive).

    ⚠️ DESTRUCTIVE and customer-facing: a live run ends a REAL listing, and its
    reviews, ranking and URL can't be recovered. Check the plan targets the
    listing you mean to retire, not the one you want to keep (compare
    channel_reference_id).

    Delete is live-proven on SHOPIFY, AMAZON and TIKTOK, on one listing each.
    Neither NextSuggestedAction "NotAllowed" nor Status "Errors while updating"
    blocks it. MAGENTO and WALMART are unproven; every row carries
    `delete_proven`.

    eBay, Etsy, Mirakl and CDiscount have no GLT templates, so a listing there has
    to be ended in the channel's own admin; passing them raises a ValueError.

    An item can have several templates on one channel (Amazon merchant + ".FBA");
    all are planned, one row each, because deleting one leaves the other live.

    Variation groups: on Shopify the PARENT holds the template, which serves every
    variant, and the children hold only the channel-SKU rows. A child is retired
    through the parent's template ONLY when no other member would lose a listing:
    every other member is already unlisted on this store, or is in `skus` or
    `also_retiring_skus`. Otherwise it is blocked as
    `variation_child_live_siblings`, naming the parent and the live siblings.
    "Live" means it has a channel-SKU row on this store, not that it has stock.
    Passing the PARENT SKU retires the whole group, with the children named in the
    plan. A child that holds its own template (usual on TikTok) is retired on its
    own.

    After a live run each template is re-opened to confirm it is gone, and the
    channel-SKU rows of every item it served are re-read. Each result's `outcome`
    is taken_down, listing_gone_template_orphaned, delete_failed (nothing removed;
    the listing may still be selling), template_deleted_listing_row_remains
    (usually channel-sync lag) or unconfirmed (the read-back failed;
    `taken_down` is None, never True).

    Batches over 10 SKUs stage: the plan comes back first, and you confirm with
    confirmed_count=<N>.

    Args:
        skus: Exact SKUs whose listings to take down.
        sub_source: Store or account to take the listing down from. Shopify: the
            store name. Amazon: the account, or a regional sub-source
            ("<account> - Germany"), which resolves to the account. TikTok: the
            shop name.
        channel: "Shopify" (default), "Amazon", "TikTok", "Magento" or "Walmart".
        allow_variation_parent_takedown: Default True (see Variation groups). False
            refuses every parent-level take-down; such children are blocked as
            `variation_child_parent_takedown_disabled`.
        also_retiring_skus: SKUs going away in the same operation. They count as
            already retired when judging a variation parent, but are NOT delisted
            themselves.
        confirmed_count: For batches over 10, pass len(skus) after reviewing the
            plan.
        dry_run: Default True: returns the plan and takes nothing down.

    Returns:
        A dict with dry_run, item_count, target_channel, target_source,
        target_sub_source, target_channel_id, sub_source_resolution,
        delete_proven, available_sub_sources;
        plan: one row per template (sku, covers_skus, stock_item_id,
            template_stock_item_id, listing_sids, title, template_id,
            configurator_id, active_listing_id, status, channel_reference_id,
            listed_quantity, next_suggested_action, action, plus
            via_variation_parent / variation_parent_sku / variation_group_name /
            group_member_skus on a parent take-down);
        unresolved: per-SKU rows with a `blocked_reason`: not_found ·
            channel_read_failed · not_listed · no_glt_template ·
            variation_child_live_siblings · variation_parent_has_no_template ·
            variation_child_parent_takedown_disabled · template_locked;
        blocked_summary, blocked_count, retirable_sku_count — read these, because a
            small plan against a large request is not a success;
        results (live run only): processed, taken_down, still_listed, outcome,
            error.
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")
    _check_injection("channel", channel or "")

    target = _resolve_glt_target(channel, sub_source)
    ch = target["channel"]
    available_sub_sources = target["available_sub_sources"]
    if not target["ok"]:
        return {"error": target["error"], "available_sub_sources": available_sub_sources}
    target_channel_id = target["channel_id"]
    channel_source = ch["source"]

    retiring: set[str] = {(s or "").strip().lower() for s in skus if (s or "").strip()}
    for s in (also_retiring_skus or []):
        _check_injection("also_retiring_skus", s or "")
        if (s or "").strip():
            retiring.add(s.strip().lower())

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    _variation_cache: dict[str, dict | None] = {}

    def _group_of_child(child_sku: str, child_sid: str) -> dict | None:
        key = child_sku.strip().lower()
        if key in _variation_cache:
            return _variation_cache[key]
        try:
            rel = _resolve_variation(child_sku, child_sid)
        except RuntimeError:
            _variation_cache[key] = None
            return None
        entry = None
        if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
            members = [{"sku": child_sku, "stock_item_id": child_sid}] + [
                {"sku": s.get("sku"), "stock_item_id": s.get("stock_item_id")}
                for s in rel.get("siblings", []) if s.get("sku")
            ]
            entry = {
                "parent_sku": rel.get("parent_sku"),
                "parent_sid": rel.get("parent_stock_item_id"),
                "group_name": rel.get("group_name"),
                "members":    members,
            }
            for m in members:
                _variation_cache[(m["sku"] or "").strip().lower()] = entry
        _variation_cache[key] = entry
        return entry

    resolved: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []
    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "blocked_reason": "empty_sku", "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": "found but returned no StockItemId"})
            continue
        title = item.get("ItemTitle")
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "blocked_reason": "channel_read_failed",
                "error": f"could not read channel listings: {exc}",
            })
            continue
        on_store = _rows_on_store(rows)
        if on_store:
            row0 = on_store[0]
            resolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "channel_reference_id": row0.get("ChannelReferenceId"),
                "listed_quantity":      row0.get("ListedQuantity"),
                "channel_row_count":    len(on_store),
                "listing_sids":         [sid],
            })
            continue

        try:
            rel = _resolve_variation(sku, sid)
        except RuntimeError:
            rel = {"role": "none"}
        if rel.get("role") == "parent" and rel.get("children"):
            child_map = _fetch_channel_skus_for_ids(
                [c["stock_item_id"] for c in rel["children"] if c.get("stock_item_id")]
            )
            listed_children = [
                c for c in rel["children"]
                if _rows_on_store(child_map.get((c.get("stock_item_id") or "").lower(), []))
            ]
            if listed_children:
                resolved.append({
                    "sku": sku, "stock_item_id": sid, "title": title,
                    "channel_reference_id": None,
                    "listed_quantity":      None,
                    "channel_row_count":    0,
                    "listing_sids":         [c["stock_item_id"] for c in listed_children],
                    "is_variation_parent":  True,
                    "variation_group_name": rel.get("group_name"),
                    "listed_via_children":  [c["sku"] for c in listed_children],
                })
                continue

        unresolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "blocked_reason": "not_listed",
            "error": (
                f"not listed on {ch['channel_type']} '{sub_source}' — nothing to take down"
            ),
        })

    templates_by_sid: dict[str, list[dict]] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    child_fallback: dict[str, dict] = {}
    variation_block: dict[str, dict] = {}
    _group_live_cache: dict[str, list[str]] = {}
    parent_sids_to_open: list[str] = []
    for r in resolved:
        if templates_by_sid.get(r["stock_item_id"].lower()) or r.get("is_variation_parent"):
            continue
        grp = _group_of_child(r["sku"], r["stock_item_id"])
        if not grp or not grp.get("parent_sid"):
            continue
        psid = grp["parent_sid"].lower()
        if psid in _group_live_cache:
            live_outside = _group_live_cache[psid]
        else:
            outside = [m for m in grp["members"]
                       if (m["sku"] or "").strip().lower() not in retiring]
            sib_map = _fetch_channel_skus_for_ids(
                [m["stock_item_id"] for m in outside if m.get("stock_item_id")]
            ) if outside else {}
            live_outside = [
                m["sku"] for m in outside
                if _rows_on_store(sib_map.get((m.get("stock_item_id") or "").lower(), []))
            ]
            _group_live_cache[psid] = live_outside

        key = r["sku"].strip().lower()
        if live_outside:
            variation_block[key] = {
                "blocked_reason":     "variation_child_live_siblings",
                "parent_sku":         grp["parent_sku"],
                "group_name":         grp["group_name"],
                "live_siblings":      live_outside,
            }
        elif not allow_variation_parent_takedown:
            variation_block[key] = {
                "blocked_reason":     "variation_child_parent_takedown_disabled",
                "parent_sku":         grp["parent_sku"],
                "group_name":         grp["group_name"],
                "live_siblings":      [],
            }
        else:
            child_fallback[key] = grp
            if psid not in templates_by_sid:
                parent_sids_to_open.append(grp["parent_sid"])

    parent_sids_to_open = list(dict.fromkeys(parent_sids_to_open))
    for i in range(0, len(parent_sids_to_open), 200):
        chunk = parent_sids_to_open[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    plan: list[dict] = []
    plan_by_template: dict = {}
    for r in resolved:
        key = r["sku"].strip().lower()
        templates = templates_by_sid.get(r["stock_item_id"].lower()) or []
        via = None
        if not templates:
            via = child_fallback.get(key)
            if via:
                templates = templates_by_sid.get(via["parent_sid"].lower()) or []
                if not templates:
                    unresolved.append({
                        **r,
                        "blocked_reason":       "variation_parent_has_no_template",
                        "variation_parent_sku": via["parent_sku"],
                        "variation_group_name": via["group_name"],
                        "error": (
                            f"variation child — its parent '{via['parent_sku']}' has no GLT "
                            "template either, so nothing here can reach this listing. End it in "
                            f"the {ch['channel_type']} admin."
                        ),
                    })
                    continue

        if not templates:
            blocked = variation_block.get(key)
            if blocked and blocked["blocked_reason"] == "variation_child_live_siblings":
                sibs = blocked["live_siblings"]
                unresolved.append({
                    **r,
                    "blocked_reason":       blocked["blocked_reason"],
                    "variation_parent_sku": blocked["parent_sku"],
                    "variation_group_name": blocked["group_name"],
                    "live_sibling_count":   len(sibs),
                    "live_siblings":        sibs[:25],
                    "error": (
                        f"variation child — its listing is owned by parent "
                        f"'{blocked['parent_sku']}', whose template still serves {len(sibs)} live "
                        "sibling listing(s). Deleting it would take those down too. Remove this "
                        f"variant in the {ch['channel_type']} admin, or retire the whole group "
                        "(pass every member, or list them in also_retiring_skus)."
                    ),
                })
            elif blocked:
                unresolved.append({
                    **r,
                    "blocked_reason":       blocked["blocked_reason"],
                    "variation_parent_sku": blocked["parent_sku"],
                    "variation_group_name": blocked["group_name"],
                    "error": (
                        f"variation child — the whole group is retirable via parent "
                        f"'{blocked['parent_sku']}', but allow_variation_parent_takedown=False. "
                        "Set it True to take the parent template down."
                    ),
                })
            else:
                unresolved.append({
                    **r,
                    "blocked_reason": "no_glt_template",
                    "error": (
                        "listed on the channel but NO GLT template exists for it (checked the "
                        "item and its variation parent) — it was most likely listed outside the "
                        f"GLT, so nothing here can reach it. End it in the {ch['channel_type']} "
                        "admin."
                    ),
                })
            continue

        for t in templates:
            tid = t.get("Id")
            existing = plan_by_template.get(tid)
            if existing is not None:
                if r["sku"] not in existing["covers_skus"]:
                    existing["covers_skus"].append(r["sku"])
                for s in (r.get("listing_sids") or []):
                    if s not in existing["listing_sids"]:
                        existing["listing_sids"].append(s)
                continue

            if t.get("IsLocked"):
                unresolved.append({
                    **r, "template_id": tid,
                    "blocked_reason": "template_locked",
                    "error": "GLT template is locked — cannot take it down right now",
                })
                continue

            info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
            row = {
                "sku":                   r["sku"],
                "covers_skus":           [r["sku"]],
                "stock_item_id":         r["stock_item_id"],
                "template_stock_item_id": via["parent_sid"] if via else r["stock_item_id"],
                "listing_sids":          list(r.get("listing_sids") or []),
                "title":                 r["title"],
                "channel":               ch["channel_type"],
                "sub_source":            sub_source,
                "template_id":           tid,
                "configurator_id":       t.get("ConfiguratorId"),
                "active_listing_id":     _glt_field(info, "ActiveListingId"),
                "status":                _glt_field(info, "Status"),
                "channel_reference_id":  r["channel_reference_id"],
                "listed_quantity":       r["listed_quantity"],
                "next_suggested_action": t.get("NextSuggestedAction"),
                "templates_on_item":     len(templates),
                "delete_proven":         ch["delete_proven"],
                "action":                "Delete",
            }
            if via:
                row.update({
                    "via_variation_parent": True,
                    "variation_parent_sku": via["parent_sku"],
                    "variation_group_name": via["group_name"],
                    "group_member_skus":    [m["sku"] for m in via["members"]],
                    "warning": (
                        f"Whole-group take-down: this deletes the template on variation parent "
                        f"'{via['parent_sku']}', which ends the listing for ALL "
                        f"{len(via['members'])} member(s) of '{via['group_name']}'. Allowed "
                        "because no member outside this operation still has a listing on "
                        f"'{sub_source}'."
                    ),
                })
            elif r.get("is_variation_parent"):
                kids = r.get("listed_via_children") or []
                row.update({
                    "is_variation_parent":  True,
                    "variation_group_name": r.get("variation_group_name"),
                    "listed_via_children":  kids,
                    "warning": (
                        f"Variation PARENT: deleting this template ends the listing for all "
                        f"{len(kids)} currently-listed child variant(s) — {', '.join(kids[:10])}"
                        f"{' …' if len(kids) > 10 else ''}. Nothing checks whether you meant to "
                        "keep any of them; pass the individual children instead if you did."
                    ),
                })
            plan.append(row)
            plan_by_template[tid] = row

    blocked_summary: dict[str, int] = {}
    for u in unresolved:
        code = u.get("blocked_reason") or "unknown"
        blocked_summary[code] = blocked_summary.get(code, 0) + 1

    base_out = {
        "item_count":            len(skus),
        "target_channel":        ch["channel_type"],
        "target_source":         channel_source,
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "sub_source_resolution": target["resolution"],
        "delete_proven":         ch["delete_proven"],
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
        "blocked_summary":       blocked_summary,
        "blocked_count":         len(unresolved),
        "retirable_sku_count":   len({s for row in plan for s in row["covers_skus"]}),
        "rate_limited":          rate_limited,
        "complete":              not rate_limited,
    }

    guard = _write_guard("unpublish_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    unproven_note = (
        ""
        if ch["delete_proven"]
        else (
            f" ⚠️  ProcessTemplates Delete is NOT yet live-proven on {ch['channel_type']} "
            f"(proven: {_proven_delete_channels()}) — prove it on ONE throwaway listing "
            "before any bulk run."
        )
    )

    blocked_note = (
        ""
        if not unresolved
        else (
            f" ⚠️  {len(unresolved)} of {len(skus)} requested SKU(s) CANNOT be taken down: "
            + ", ".join(f"{n}× {code}" for code, n in
                        sorted(blocked_summary.items(), key=lambda kv: -kv[1]))
            + ". See unresolved[] — each row carries blocked_reason and what to do instead "
              "(variation_child_live_siblings and no_glt_template need the channel's own admin)."
        )
    )
    group_note = (
        ""
        if not any(p.get("via_variation_parent") for p in plan)
        else (
            f" {sum(1 for p in plan if p.get('via_variation_parent'))} of these are WHOLE-GROUP "
            "take-downs via a variation parent — allowed only because no member outside this "
            "operation is still listed; check plan[].group_member_skus."
        )
    )

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing taken down. {len(plan)} {ch['channel_type']} template(s) on "
                f"'{sub_source}' would be DELETED (listing ended), covering "
                f"{base_out['retirable_sku_count']} of {len(skus)} requested SKU(s). Review the "
                "plan — confirm each channel_reference_id / listed_quantity is the orphan you "
                "mean to retire, NOT a listing you want to keep — then set dry_run=False. A live "
                f"run removes real customer-facing listings.{group_note}{blocked_note}"
                f"{unproven_note}"
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": (
                f"Nothing to take down — none of the {len(skus)} requested SKU(s) resolved to a "
                f"deletable {ch['channel_type']} template.{blocked_note}"
            ),
        }

    results: list[dict] = []
    for row in plan:
        res = {
            "sku":         row["sku"],
            "covers_skus": list(row.get("covers_skus") or [row["sku"]]),
            "channel":     ch["channel_type"],
            "sub_source":  sub_source,
            "template_id": row["template_id"],
            "via_variation_parent": bool(row.get("via_variation_parent")),
            "action":      "Delete",
            "processed":   False,
            "taken_down":  None,
        }
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
                    "TemplateRequests": [
                        {"TemplateId": row["template_id"], "Action": "Delete"}
                    ],
                    "ClientContext": {"Activity": "unpublish_channel_listing", "Source": "linnworks-plugin"},
                }},
            )
            res["processed"] = True
        except RuntimeError as exc:
            res["error"] = f"ProcessTemplates (Delete) failed: {exc}"
        results.append(res)

    remaining: dict = {}
    template_readback_error: dict[str, str] = {}
    for tsid in dict.fromkeys(row["template_stock_item_id"] for row in plan):
        try:
            resp = call_linnworks(
                "GenericListings/OpenTemplatesByInventory",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
                    "Parameters": {
                        "SelectedRegions": [],
                        "Token": _ZERO_GUID,
                        "InventoryItemIds": [tsid],
                        "ChannelId": target_channel_id,
                    },
                    "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 20},
                }},
            )
            for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
                info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
                remaining[t.get("Id")] = _glt_field(info, "Status")
        except RuntimeError as exc:
            template_readback_error[tsid] = str(exc)

    listing_counts: dict[str, int] = {}
    listing_errors: dict[str, str] = {}
    for lsid in dict.fromkeys(s for row in plan for s in (row.get("listing_sids") or [])):
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": lsid},
            )
            listing_counts[lsid] = len(_rows_on_store(rows))
        except RuntimeError as exc:
            listing_errors[lsid] = str(exc)

    taken = 0
    for row, r in zip(plan, results):
        lsids = row.get("listing_sids") or []
        errs = [listing_errors[s] for s in lsids if s in listing_errors]
        if errs:
            r["readback_error"] = f"could not confirm take-down: {errs[0]}"
        still_count = (
            sum(listing_counts[s] for s in lsids)
            if lsids and all(s in listing_counts for s in lsids)
            else None
        )
        if still_count is not None:
            r["still_listed"] = still_count > 0

        terr = template_readback_error.get(row["template_stock_item_id"])
        if terr:
            r["template_readback_error"] = terr
            r["taken_down"] = None
            r["outcome"] = "unconfirmed"
            continue

        tid = r["template_id"]
        template_gone = tid not in remaining
        r["template_deleted"] = template_gone
        r["template_status_after"] = remaining.get(tid)

        if still_count is None:
            r["taken_down"] = None
            r["outcome"] = "unconfirmed"
        elif template_gone and still_count == 0:
            r["taken_down"] = True
            r["outcome"] = "taken_down"
            taken += 1
        elif template_gone and still_count:
            r["taken_down"] = False
            r["outcome"] = "template_deleted_listing_row_remains"
        elif not template_gone and still_count == 0:
            r["taken_down"] = False
            r["outcome"] = "listing_gone_template_orphaned"
            r["warning"] = (
                f"Template {tid} still exists (status "
                f"{r['template_status_after']!r}) even though the channel-SKU row is gone. "
                "The listing itself appears to be down — verify in the channel's admin. "
                "Repeated Delete calls on such a template return 2xx and change nothing."
            )
        else:
            r["taken_down"] = False
            r["outcome"] = "delete_failed"
            r["warning"] = (
                f"Template {tid} was NOT deleted (status "
                f"{r['template_status_after']!r}) and the listing row is still present — "
                "the listing may still be live. Check the channel's admin."
            )

    orphaned = sum(1 for r in results if r.get("outcome") == "listing_gone_template_orphaned")
    failed = sum(1 for r in results if r.get("outcome") == "delete_failed")
    extra = ""
    if orphaned:
        extra += (
            f" {orphaned} template(s) survived the Delete but their listing row is gone "
            "(orphaned template — listing appears down; see results[].warning)."
        )
    if failed:
        extra += (
            f" ⚠️ {failed} template(s) were NOT deleted AND still have a live listing row — "
            "these may still be selling."
        )

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "taken_down_count": taken,
        "orphaned_template_count": orphaned,
        "delete_failed_count": failed,
        "message": (
            f"{taken}/{len(plan)} {ch['channel_type']} template(s) on '{sub_source}' confirmed "
            "taken down. Each template is verified individually (re-opened by id) AND against the "
            "channel-SKU table, because a per-item check alone reports success for templates that "
            "were never deleted on multi-template items. On a variation-parent "
            "take-down the listing side is read on the CHILDREN, since the parent has no "
            "channel-SKU rows of its own. The channel may lag, so a still_listed=true "
            "row may simply not have synced — re-check with get_channel_listings and verify in the "
            f"channel's own admin. Per-item errors are in results[].error.{extra}{blocked_note}"
            f"{unproven_note}"
        ),
    }


@_tool()
def delist_all_channel_listings(
    skus: list[str],
    channels: list[str] | None = None,
    allow_variation_parent_takedown: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take down EVERY GLT-manageable listing for each item, across all channels and
    stores it is listed on. It reads each item's channel-SKU rows, groups them by
    channel and store/account, and hands each group to unpublish_channel_listing
    (GLT Delete). It doesn't touch the item or its stock. Items must be ACTIVE;
    for archived items, unarchive in the Linnworks UI, run this, let the channels
    sync, then archive_inventory_items.

    ⚠️ DESTRUCTIVE and customer-facing: ends real listings, and their reviews,
    ranking and URL can't be recovered. Delete is live-proven on Shopify, Amazon
    and TikTok (one listing each); every row carries `delete_proven`.

    ⚠️ "EVERYWHERE" IS NEVER LITERALLY TRUE. eBay, Etsy, Mirakl and CDiscount have
    no GLT templates, and nor does any channel with no configurators in your
    account, so those listings are reported under `skipped_channels` and LEFT UP
    for manual take-down in the channel's own admin.

    ⚠️ Amazon's regional sub-sources ("<account> - Germany", "- Spain", …) share
    one account and one set of templates, so they collapse into ONE take-down per
    item and account (`covers_sub_sources`). After a live run, any region still
    listed is reported in `still_listed_sub_sources`.

    ⚠️ MOST DEAD VARIANTS INSIDE LIVE PRODUCTS CAN'T BE RETIRED HERE. On Shopify a
    variant's listing belongs to its parent's template, so it can only go when
    the whole group goes. That happens automatically when every other member is
    dead or in this batch, and is refused (`variation_child_live_siblings`) when
    a sibling is still listed. Expect a large `blocked_summary`; those variants
    have to be removed on the channel. Read `blocked_reason` per SKU rather than
    treating the take-down count as a completion rate.

    Batches over 10 planned take-downs stage: confirm with the take_down_count
    from the dry run.

    Args:
        skus: Exact, ACTIVE SKUs to delist. The list is treated as one
            operation, so a variation group passed in full can be retired.
        channels: GLT channels to act on, e.g. ["Shopify", "Amazon"]. Default:
            every GLT channel with configurators in your account.
        allow_variation_parent_takedown: Passed to unpublish_channel_listing.
            Default True; False refuses every whole-group take-down.
        confirmed_count: For over 10 planned take-downs, the take_down_count from
            the dry-run manifest.
        dry_run: Default True: preview only.

    Returns:
        A dict with glt_channels (what is actionable in your account), per-SKU
        discovery (targets and skipped_channels), a combined plan of template
        take-downs, take_down_count, unresolved (each with `blocked_reason`),
        blocked_summary, blocked_count, retirable_sku_count, and on a live run
        per-template results plus still_listed_sub_sources.
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    if channels is None:
        wanted = list(GLT_CHANNELS.keys())
    else:
        wanted = []
        for c in channels:
            _check_injection("channels", c or "")
            wanted.append(_resolve_glt_channel(c)["key"])

    channel_state: dict[str, dict] = {}
    for key in dict.fromkeys(wanted):
        entry = GLT_CHANNELS[key]
        try:
            cat = _fetch_glt_configurators(entry["channel_type"])
        except (RuntimeError, ValueError):
            cat = []
        ss_to_channel: dict[str, int] = {}
        for c in cat:
            ss, cid = c.get("sub_source"), c.get("channel_id")
            if ss and cid is not None:
                ss_to_channel.setdefault(_norm_conf_name(ss), cid)
        channel_state[_norm_conf_name(entry["source"])] = {
            **entry, "key": key,
            "ss_to_channel": ss_to_channel,
            "accounts": sorted({c["sub_source"] for c in cat if c.get("sub_source")}),
        }

    discovery: list[dict] = []
    unresolved: list[dict] = []
    work: list[dict] = []
    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku,
                "error": (
                    f"not found / not resolvable: {exc}. If this item is ARCHIVED, "
                    "unarchive it in Linnworks first — archived SKUs cannot be resolved."
                ),
            })
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "stock_item_id": sid,
                               "error": f"could not read channel listings: {exc}"})
            continue
        rows = rows if isinstance(rows, list) else []

        targets: dict[tuple[str, int], dict] = {}
        skipped: list[dict] = []
        for r in rows:
            src, ss = r.get("Source"), r.get("SubSource")
            state = channel_state.get(_norm_conf_name(src))
            if state is None:
                skipped.append({
                    "source": src, "sub_source": ss,
                    "channel_reference_id": r.get("ChannelReferenceId"),
                    "reason": (
                        "not a GLT channel — no template, so it cannot be ended here; end it in "
                        "the channel's own admin"
                        if _norm_conf_name(src) in {_norm_conf_name(s) for s in NON_GLT_SOURCES}
                        else "channel not selected / not GLT-manageable in this account"
                    ),
                })
                continue
            cid, resolution = _glt_channel_id_for(state["ss_to_channel"], ss or "")
            if cid is None:
                skipped.append({
                    "source": src, "sub_source": ss,
                    "channel_reference_id": r.get("ChannelReferenceId"),
                    "reason": (
                        f"no {state['channel_type']} GLT configurator matches this sub-source "
                        f"(accounts here: {state['accounts'] or 'none'})"
                    ),
                })
                continue
            k = (state["channel_type"], cid)
            t = targets.setdefault(k, {
                "channel": state["channel_type"],
                "channel_id": cid,
                "sub_source": ss,
                "sub_source_resolution": resolution,
                "covers_sub_sources": [],
                "delete_proven": state["delete_proven"],
            })
            if ss and ss not in t["covers_sub_sources"]:
                t["covers_sub_sources"].append(ss)
            if resolution == "exact" and t["sub_source_resolution"] != "exact":
                t["sub_source"] = ss
                t["sub_source_resolution"] = "exact"

        for t in targets.values():
            work.append({"sku": sku, "channel_type": t["channel"], "sub_source": t["sub_source"]})
        discovery.append({
            "sku": sku, "stock_item_id": sid, "title": item.get("ItemTitle"),
            "targets": list(targets.values()), "skipped_channels": skipped,
        })

    batch_skus = [(s or "").strip() for s in skus if (s or "").strip()]
    by_target: dict[tuple, dict] = {}
    for w in work:
        k = (w["channel_type"], _norm_conf_name(w["sub_source"]))
        t = by_target.setdefault(k, {"channel_type": w["channel_type"],
                                     "sub_source": w["sub_source"], "skus": []})
        if w["sku"] not in t["skus"]:
            t["skus"].append(w["sku"])
    targets_work = list(by_target.values())

    plan: list[dict] = []
    for w in targets_work:
        sub = unpublish_channel_listing(
            skus=w["skus"], sub_source=w["sub_source"], channel=w["channel_type"],
            allow_variation_parent_takedown=allow_variation_parent_takedown,
            also_retiring_skus=batch_skus,
            confirmed_count=len(w["skus"]), dry_run=True,
        )
        if sub.get("error"):
            unresolved.append({**w, "error": sub["error"]})
            continue
        plan.extend(sub.get("plan", []))
        for u in sub.get("unresolved", []):
            unresolved.append({**u, "channel": w["channel_type"], "sub_source": w["sub_source"]})

    glt_channels = [
        {"channel": s["channel_type"], "accounts": s["accounts"],
         "actionable": bool(s["accounts"]), "delete_proven": s["delete_proven"]}
        for s in channel_state.values()
    ]
    blocked_summary: dict[str, int] = {}
    for u in unresolved:
        code = u.get("blocked_reason") or "unknown"
        blocked_summary[code] = blocked_summary.get(code, 0) + 1

    base_out = {
        "item_count":       len(skus),
        "glt_channels":     glt_channels,
        "discovery":        discovery,
        "plan":             plan,
        "unresolved":       unresolved,
        "blocked_summary":  blocked_summary,
        "blocked_count":    len(unresolved),
        "retirable_sku_count": len({s for p in plan for s in (p.get("covers_skus") or [p["sku"]])}),
        "skipped_channels": [
            {"sku": d["sku"], **s} for d in discovery for s in d["skipped_channels"]
        ],
    }

    guard = _write_guard("delist_all_channel_listings", plan, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    unproven = sorted({p["channel"] for p in plan if not p.get("delete_proven")})
    unproven_note = (
        f" ⚠️  {', '.join(unproven)} Delete is NOT live-proven — prove it on one throwaway "
        "listing per channel first."
        if unproven else ""
    )

    blocked_note = (
        ""
        if not unresolved
        else (
            f" ⚠️  {base_out['blocked_count']} SKU-listing(s) CANNOT be taken down: "
            + ", ".join(f"{n}× {code}" for code, n in
                        sorted(blocked_summary.items(), key=lambda kv: -kv[1]))
            + ". See unresolved[].blocked_reason — variation_child_live_siblings and "
              "no_glt_template both need removing on the channel side, not here."
        )
    )
    group_note = (
        ""
        if not any(p.get("via_variation_parent") for p in plan)
        else (
            f" {sum(1 for p in plan if p.get('via_variation_parent'))} row(s) are WHOLE-GROUP "
            "take-downs via a variation parent — every member is dead or in this batch; check "
            "plan[].group_member_skus."
        )
    )

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "take_down_count": len(plan),
            "message": (
                f"Dry run — nothing taken down. {len(plan)} template(s) would be DELETED, "
                f"covering {base_out['retirable_sku_count']} of {len(skus)} requested SKU(s); "
                f"{len(base_out['skipped_channels'])} listing(s) on non-GLT channels stay up "
                "(see skipped_channels). Review the plan, then set "
                f"dry_run=False.{group_note}{blocked_note}{unproven_note}"
            ),
        }

    results: list[dict] = []
    for w in targets_work:
        sub = unpublish_channel_listing(
            skus=w["skus"], sub_source=w["sub_source"], channel=w["channel_type"],
            allow_variation_parent_takedown=allow_variation_parent_takedown,
            also_retiring_skus=batch_skus,
            confirmed_count=len(w["skus"]), dry_run=False,
        )
        results.extend(sub.get("results", []))

    still_listed: list[dict] = []
    for d in discovery:
        if not d["targets"]:
            continue
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": d["stock_item_id"]}
            )
        except RuntimeError as exc:
            still_listed.append({"sku": d["sku"], "readback_error": str(exc)})
            continue
        rows = rows if isinstance(rows, list) else []
        for t in d["targets"]:
            src = GLT_CHANNELS[_norm_conf_name(t["channel"])]["source"]
            remaining = sorted({
                r.get("SubSource") for r in rows
                if _norm_conf_name(r.get("Source")) == _norm_conf_name(src)
                and r.get("SubSource") in t["covers_sub_sources"]
            })
            if remaining:
                still_listed.append({
                    "sku": d["sku"], "channel": t["channel"], "sub_sources": remaining,
                })

    taken = sum(1 for r in results if r.get("taken_down"))
    orphaned = sum(1 for r in results if r.get("outcome") == "listing_gone_template_orphaned")
    failed = sum(1 for r in results if r.get("outcome") == "delete_failed")
    extra = ""
    if orphaned:
        extra += (f" {orphaned} orphaned template(s): the Delete did not remove them but their "
                  "listing row is gone — listing appears down, template needs manual tidying.")
    if failed:
        extra += (f" ⚠️ {failed} template(s) were NOT deleted and still have a live listing row — "
                  "these may still be selling; check the channel's admin.")
    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "still_listed_sub_sources": still_listed,
        "taken_down_count": taken,
        "orphaned_template_count": orphaned,
        "delete_failed_count": failed,
        "message": (
            f"{taken}/{len(results)} template(s) confirmed taken down across all selected "
            f"channels. {len(base_out['skipped_channels'])} listing(s) on non-GLT channels were "
            "left up (see skipped_channels) — those need ending in the channel's own admin. "
            "still_listed_sub_sources lists any store/region whose channel-SKU row survived; "
            "channel sync can lag, so re-check with get_channel_listings before concluding the "
            f"take-down failed.{extra}{group_note}{blocked_note}{unproven_note}"
        ),
    }


@_tool()
def delist_all_shopify_listings(
    skus: list[str],
    allow_variation_parent_takedown: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take down every SHOPIFY listing for each given item, across all Shopify stores
    it is listed on — the Shopify-scoped, live-proven slice of
    delist_all_channel_listings.

    Identical behaviour and safety posture, with `channels` pinned to Shopify:
    non-Shopify listings (Amazon, TikTok, eBay, Mirakl…) are reported under
    `skipped_channels` and LEFT UP. Use `delist_all_channel_listings` to also
    retire Amazon/TikTok (GLT) listings — noting those deletes are not yet
    live-proven.

    ⚠️  DESTRUCTIVE and customer-facing. dry_run=True by default; staging
    threshold 10 on the number of planned take-downs.

    Args:
        skus: Exact SKUs / ItemNumbers (must be ACTIVE / resolvable).
        allow_variation_parent_takedown: True (default) allows a whole-group
            take-down via the variation parent when no member outside this batch
            is still listed; False refuses every parent-level take-down. See
            delist_all_channel_listings.
        confirmed_count: For > 10 planned take-downs, pass the take_down_count
            from the dry-run manifest to confirm.
        dry_run: If True (default), preview only. Set False to execute.
    """
    return delist_all_channel_listings(
        skus=skus, channels=["Shopify"],
        allow_variation_parent_takedown=allow_variation_parent_takedown,
        confirmed_count=confirmed_count, dry_run=dry_run,
    )


@_tool()
def archive_inventory_items(
    skus: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    ARCHIVE inventory items (the Linnworks "archive" action) by SKU.

    Archiving hides an item from the active catalogue. It is REVERSIBLE via
    unarchive_inventory_items (or the Linnworks UI). Typically the final step of
    archived-item cleanup: after you unarchive items to delist them
    (delist_all_shopify_listings), re-archive them here.

    Items must be ACTIVE to resolve by SKU (an already-archived SKU won't resolve
    → reported unresolved). Read-before-write resolves each SKU → StockItemId and
    captures the title; read-back confirms the SKU no longer resolves (i.e. it is
    archived). Uses Inventory/ArchiveInventoryItems (one batched call).

    ⚠️  Archiving an item can affect its channel behaviour (a stale listing may be
    left frozen — see the archived-item listing gotcha). Delist first, then archive.

    Args:
        skus: Exact SKUs / ItemNumbers to archive (must be ACTIVE).
        confirmed_count: For > 25 items, pass len(skus) after reviewing the manifest.
        dry_run: If True (default), preview only. Set False to archive.

    Returns:
        dict with manifest (per-SKU sku, stock_item_id, title, status) plus
        unresolved rows, and — on a live run — per-SKU `archived` read-back.
    """
    return _set_archive_state(skus, archive=True,
                              confirmed_count=confirmed_count, dry_run=dry_run)


@_tool()
def unarchive_inventory_items(
    stock_item_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    UNARCHIVE (restore to active) inventory items by StockItemId GUID.

    ⚠️  Takes StockItemId GUIDs, NOT SKUs — an archived item CANNOT be resolved
    from its SKU (GetInventoryItem fails, and no SKU→id resolver sees archived
    items), so the only handle is the GUID. Source the GUIDs from a Linnworks
    export of the Archived view that includes the "Stock Item ID" column. There is
    no API to enumerate archived items.

    Restoring is REVERSIBLE via archive_inventory_items. Once unarchived, an item
    becomes active and resolves by SKU again — so you can then delist it
    (delist_all_shopify_listings) and re-archive it. Uses
    Inventory/UnarchiveInventoryItems (one batched call). Read-back confirms each
    id now resolves as an active item.

    Args:
        stock_item_ids: StockItemId GUIDs to unarchive.
        confirmed_count: For > 25 items, pass len(stock_item_ids) after review.
        dry_run: If True (default), preview only. Set False to unarchive.

    Returns:
        dict with manifest (per-id stock_item_id) plus, on a live run, per-id
        `active` read-back.
    """
    return _set_archive_state(stock_item_ids, archive=False,
                              confirmed_count=confirmed_count, dry_run=dry_run)


def _set_archive_state(
    items: list[str],
    archive: bool,
    confirmed_count: int | None,
    dry_run: bool,
) -> dict:
    op = "archive_inventory_items" if archive else "unarchive_inventory_items"
    endpoint = ("Inventory/ArchiveInventoryItems" if archive
                else "Inventory/UnarchiveInventoryItems")
    if not items:
        raise ValueError("The list must contain at least one item.")

    manifest: list[dict] = []
    unresolved: list[dict] = []
    ids: list[str] = []
    cache: dict = {}
    for raw in items:
        val = (raw or "").strip()
        if not val:
            unresolved.append({"input": raw, "error": "empty value"})
            continue
        if archive:
            try:
                item = call_linnworks("Inventory/GetInventoryItem", {"sku": val})
            except RuntimeError as exc:
                unresolved.append({"sku": val, "error": (
                    f"not resolvable: {exc}. Already archived items cannot be "
                    "resolved by SKU.")})
                continue
            sid = item.get("StockItemId")
            if not sid:
                unresolved.append({"sku": val, "error": "no StockItemId returned"})
                continue
            manifest.append({"sku": val, "stock_item_id": sid,
                             "title": item.get("ItemTitle")})
            ids.append(sid)
        else:
            manifest.append({"stock_item_id": val})
            ids.append(val)

    base_out = {"item_count": len(items), "manifest": manifest, "unresolved": unresolved}

    guard = _write_guard(op, items, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        verb = "archived" if archive else "unarchived"
        return {
            "dry_run": True, **base_out,
            "message": (
                f"Dry run — nothing changed. {len(ids)} item(s) would be {verb}; "
                f"{len(unresolved)} unresolved. Set dry_run=False to execute."
            ),
        }

    if not ids:
        return {"dry_run": False, **base_out,
                "message": "Nothing to do — no items resolved."}

    call_linnworks(endpoint, {"parameters": {
        "InventoryItemIds": ids, "SelectedRegions": [], "Token": _ZERO_GUID,
    }})

    results: list[dict] = []
    for m in manifest:
        row = dict(m)
        if archive and m.get("sku"):
            try:
                call_linnworks("Inventory/GetInventoryItem", {"sku": m["sku"]})
                row["archived"] = False
            except RuntimeError:
                row["archived"] = True
        else:
            try:
                gi = call_linnworks("Inventory/GetInventoryItem",
                                    {"stockItemId": m["stock_item_id"]})
                row["active"] = bool(gi.get("StockItemId"))
            except RuntimeError:
                row["active"] = None
        results.append(row)

    verb = "archived" if archive else "unarchived"
    confirmed = sum(1 for r in results if (r.get("archived") if archive else r.get("active")))
    return {
        "dry_run": False, **base_out, "results": results,
        "message": (
            f"{verb.capitalize()} {len(ids)} item(s); {confirmed} confirmed by read-back. "
            + ("Archived SKUs no longer resolve." if archive
               else "Unarchived items now resolve as active.")
        ),
    }


EBAY_DESCRIPTION_FRAME_URL_TEMPLATE = "https://vi.vipr.ebaydesc.com/itmdesc/{item_id}"
EBAY_NEVER_SYNCED_DATE_PREFIX = "0001-01-01"


def _ebay_effective_description(raw_rows, store: str, default_description):
    value, source = _effective_channel_value(
        raw_rows, store, "Description", channel_source="EBAY", fallback=None)
    if value:
        return value, source
    store_norm = _norm_conf_name(store)
    if store_norm:
        for r in (raw_rows if isinstance(raw_rows, list) else []):
            if (_norm_conf_name(r.get("Source")) == "ebay"
                    and _norm_conf_name(r.get("SubSource")).startswith(store_norm)):
                val = r.get("Description")
                if val:
                    return val, "channel_override_prefix"
    return default_description, "base"


def _is_never_synced_channel_sku(last_update) -> bool:
    if not last_update:
        return True
    return str(last_update).startswith(EBAY_NEVER_SYNCED_DATE_PREFIX)


def _sweep_ebay_config_for_listing(config_id, listing_id: str) -> dict | None:
    if not config_id:
        return None
    page = 1
    while True:
        try:
            resp = call_linnworks("Listings/GeteBayTemplates", {
                "parameters": {
                    "ConfigId": config_id,
                    "PageNumber": page,
                    "EntriesPerPage": 200,
                    "TemplatesType": "Both",
                }
            })
        except RuntimeError:
            return None
        items = resp.get("Items") if isinstance(resp, dict) else None
        if not items:
            return None
        for it in items:
            if listing_id in (it.get("ListingIds") or []):
                return it
        total = resp.get("TotalItems") or 0
        if page * 200 >= total:
            return None
        page += 1


def _find_ebay_template_for_listing(listing_id: str, config_id: str | None = None) -> dict | None:
    if config_id:
        return _sweep_ebay_config_for_listing(config_id, listing_id)
    try:
        configs = call_linnworks_get("Listings/GeteBayConfigurators")
    except RuntimeError:
        return None
    if not isinstance(configs, list):
        return None
    populated = sorted(
        (c for c in configs if isinstance(c, dict) and c.get("AssociatedTemplates")),
        key=lambda c: -(c.get("AssociatedTemplates") or 0),
    )
    for cfg in populated:
        found = _sweep_ebay_config_for_listing(cfg.get("pkConfigId"), listing_id)
        if found is not None:
            return found
    return None


def _ebay_template_staleness(template: dict, current_title) -> dict:
    stored_title = template.get("Title")
    stored_price = template.get("Price")
    out: dict = {
        "stored_title": stored_title,
        "stored_price": stored_price,
        "title_stale": None,
    }
    if stored_title is not None and current_title is not None:
        out["title_stale"] = str(stored_title).strip() != str(current_title).strip()
    if out["title_stale"]:
        out["warning"] = (
            f"STALE SNAPSHOT — the stored template title ('{stored_title}') disagrees "
            f"with the item's current title ('{current_title}'). Pushing this template "
            "re-sends the STORED title/price and will overwrite the current one on the "
            "live eBay listing (the same hazard the Shopify GLT push hit in v1.27.1) — "
            "review stored_price above too before confirming."
        )
    else:
        out["warning"] = (
            "Title shows no detectable difference, but stored_price is NOT independently "
            "re-checked here — review it against the live listing before confirming."
        )
    return out


@_tool()
def revise_ebay_listing_description(
    skus: list[str],
    store: str = "EBAY0",
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Revise an EXISTING eBay listing's description to match the item's current
    eBay-channel description in Linnworks. Description only: it cannot create, end
    or relist a listing. eBay is not a GLT channel (refresh_channel_listing and
    unpublish_channel_listing refuse it), so this uses Linnworks' separate eBay
    Listings API: GeteBayConfigurators and GeteBayTemplates to read,
    ProcesseBayListings to write.

    ⚠️ NOT PROVEN TO REACH EBAY. The push was fired live on 26 Aug 2026: Linnworks
    accepted it, but the listing did not change in two hours, while the same edit
    made in the Linnworks listing UI landed within minutes. `revise_proven` stays
    False, and every live row is reported `unconfirmed`, never `success`. Until
    that changes, review the content with a dry run here and make the edit in the
    Linnworks listing UI.

    ⚠️ THE WHOLE STORED TEMPLATE IS PUSHED. Only Description is replaced; Title,
    Price and everything else go out as Linnworks last stored them, which may be
    out of date. Each plan row (dry run included) shows `staleness.stored_title`,
    `staleness.stored_price` and `title_stale`, so you can see what would go live.
    The price is shown but not checked.

    ⚠️ EBAY CAN REFUSE A REVISE (active bids, a recent sale, category rules)
    without an error: the push still returns 204. After a live push each result row
    carries `post_push_status` and `post_push_error_message` from the stored
    template; read them before treating `unconfirmed` as good news. This tool does
    not act on them. `post_push_description_matches` checks Linnworks' stored copy,
    not the live listing.

    ⚠️ A CHANNEL-SKU ROW PROVES A MAPPING, NOT A LIVE LISTING. Covered SKUs whose
    mapping has never synced (LastUpdate null or 0001-01-01) are listed in
    `never_synced_skus` with a `never_synced_warning`. They are still planned;
    review them.

    Check results on the description frame,
    https://vi.vipr.ebaydesc.com/itmdesc/<item_id>, not the item page, which can
    keep showing the old description after a push has landed.

    Shop design wrapper: stored descriptions hold only the inner copy, and the
    seller's design template adds the wrapper when a listing is pushed, so this
    tool sends no wrapper and cannot strip one. A revise made in the Linnworks UI
    kept the wrapper; an API push has not yet been seen to land at all.

    How it works: SKUs are grouped by eBay item id (ChannelReferenceId), so a
    multi-variation listing gets ONE push however many SKUs it covers
    (`covers_skus`). The description is the item's eBay-channel description for
    `store`, falling back to its default description; store names are also matched
    by prefix, because the description table can name the store "<store>_UK". An
    item with no usable description is blocked, never pushed empty. There is no
    direct lookup from listing to template, so each listing's template is found by
    sweeping the eBay configurators; that is quota-heavy, even on a dry run. A 429
    during the sweep blocks the row as "rate_limited_locating_template" and never
    reads as "not found". A listing whose template genuinely can't be found is
    blocked too, so a dry run never counts a push the live run would refuse.

    Batches over 10 stage: the plan comes back first, and you confirm with
    confirmed_count=<N>.

    Args:
        skus: Exact SKUs whose eBay listing description to revise.
        store: eBay account, matched case-insensitively against the channel-SKU
            table's SubSource. Default "EBAY0", the usual SubSource for a first
            eBay account; check yours with get_channel_listings.
        confirmed_count: For batches over 10, pass len(skus) after reviewing the
            plan.
        dry_run: Default True. Writes nothing (no ProcesseBayListings call), but
            does read the eBay templates to preview what would be pushed.

    Returns:
        A dict with dry_run, sku_count, store, revise_proven, push_observed_state,
        verification_note, message;
        plan: one row per eBay listing (listing_id, covers_skus, description,
            description_source: channel_override / channel_override_prefix / base,
            blocked, blocked_reason, never_synced_skus, never_synced_warning,
            template_found, staleness, revise_proven, push_observed_state);
        not_listed: SKUs with no eBay channel-SKU row for `store`;
        unresolved: SKUs that failed to resolve;
        rate_limited: anything that hit the Linnworks quota, at any step (never
            counted as not found), and complete: False when it is non-empty;
        results (live run only): outcome unconfirmed / blocked / push_failed /
            rate_limited (never success), post_push_description_matches,
            post_push_read_back_rate_limited, post_push_status,
            post_push_error_message.
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")
    _check_injection("store", store or "")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, None)

    not_listed: list[dict] = []
    planned_skus: list[dict] = []

    channel_rows_by_sid: dict[str, list] = {}
    if resolved:
        sid_list = [sid for _sku, sid in resolved]
        try:
            channel_rows_by_sid = _fetch_channel_skus_for_ids(sid_list)
        except RateLimitError as exc:
            for sku, _sid in resolved:
                rate_limited.append({"sku": sku, "error": str(exc)})
            resolved = []
        except RuntimeError as exc:
            for sku, _sid in resolved:
                unresolved.append({"sku": sku, "error": f"channel-SKU read failed: {exc}"})
            resolved = []

    for sku, sid in resolved:
        rows = channel_rows_by_sid.get(sid.lower(), [])
        ebay_rows = [
            r for r in rows
            if _norm_conf_name(r.get("Source")) == "ebay"
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(store)
        ]
        if not ebay_rows:
            not_listed.append({
                "sku": sku, "stock_item_id": sid,
                "reason": f"no eBay channel-SKU row for store '{store}'",
            })
            continue
        listing_id = ebay_rows[0].get("ChannelReferenceId")
        if not listing_id:
            not_listed.append({
                "sku": sku, "stock_item_id": sid,
                "reason": "eBay channel-SKU row has no ChannelReferenceId (listing id)",
            })
            continue
        planned_skus.append({
            "sku": sku, "sid": sid, "listing_id": listing_id,
            "last_update": ebay_rows[0].get("LastUpdate"),
        })

    by_listing: dict[str, dict] = {}
    for row in planned_skus:
        entry = by_listing.setdefault(row["listing_id"], {
            "listing_id": row["listing_id"], "covers_skus": [], "sids": [],
            "never_synced_skus": [],
        })
        entry["covers_skus"].append(row["sku"])
        entry["sids"].append(row["sid"])
        if _is_never_synced_channel_sku(row["last_update"]):
            entry["never_synced_skus"].append(row["sku"])

    template_by_listing: dict[str, dict | None] = {}

    plan: list[dict] = []
    for listing_id, entry in by_listing.items():
        description = None
        description_source = None
        blocked = False
        blocked_reason = None
        mismatched: list[str] = []
        for sku, sid in zip(entry["covers_skus"], entry["sids"]):
            try:
                raw_desc_rows = call_linnworks_get(
                    "Inventory/GetInventoryItemDescriptions", {"inventoryItemId": sid})
            except RateLimitError as exc:
                rate_limited.append({"sku": sku, "error": str(exc)})
                blocked = True
                blocked_reason = "rate_limited_reading_description"
                continue
            except RuntimeError as exc:
                blocked = True
                blocked_reason = f"description read failed: {exc}"
                continue
            default_desc = next(
                (r.get("Description") for r in raw_desc_rows
                 if isinstance(raw_desc_rows, list) and not r.get("Source")
                 and not r.get("SubSource")),
                None,
            )
            sku_desc, sku_desc_source = _ebay_effective_description(
                raw_desc_rows, store, default_desc)
            if not sku_desc or not str(sku_desc).strip():
                blocked = True
                blocked_reason = blocked_reason or f"no usable description for SKU '{sku}'"
                continue
            if description is None:
                description, description_source = sku_desc, sku_desc_source
            elif sku_desc != description:
                mismatched.append(sku)

        if description is None and not blocked:
            blocked = True
            blocked_reason = "no usable description found for any covered SKU"

        row_out = {
            "listing_id": listing_id,
            "covers_skus": entry["covers_skus"],
            "description": description,
            "description_source": description_source,
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "never_synced_skus": entry["never_synced_skus"],
            "revise_proven": EBAY_CHANNELS["ebay"]["revise_proven"],
            "push_observed_state": EBAY_CHANNELS["ebay"]["push_observed_state"],
        }
        if entry["never_synced_skus"]:
            row_out["never_synced_warning"] = (
                f"{len(entry['never_synced_skus'])} covered SKU(s) "
                f"({', '.join(entry['never_synced_skus'])}) have an eBay channel-SKU "
                "LastUpdate of 0001-01-01 (or missing) — this mapping has never been "
                "confirmed by the channel, so the listing may not actually be live even "
                "though it resolved a listing id."
            )
        if mismatched:
            row_out["description_mismatch_skus"] = mismatched
            row_out["warning"] = (
                "Covered SKUs disagree on eBay description content — pushing the first "
                "resolved SKU's value; review before a live run."
            )

        if not blocked:
            try:
                template = _find_ebay_template_for_listing(listing_id)
            except RateLimitError as exc:
                rate_limited.append({
                    "listing_id": listing_id, "skus": entry["covers_skus"],
                    "error": str(exc),
                })
                template = None
                template_by_listing[listing_id] = None
                blocked = True
                blocked_reason = "rate_limited_locating_template"
                row_out["blocked"] = True
                row_out["blocked_reason"] = blocked_reason
                row_out["template_found"] = None
                row_out["staleness"] = None
            else:
                template_by_listing[listing_id] = template
                row_out["template_found"] = template is not None
                if template is not None:
                    first_sid = entry["sids"][0]
                    current_title = titles.get(first_sid.lower())
                    row_out["staleness"] = _ebay_template_staleness(template, current_title)
                else:
                    row_out["staleness"] = None
                    row_out["stale_snapshot_warning"] = (
                        "Could not locate the eBay template for this listing — stored "
                        "Title/Price cannot be previewed."
                    )
                    blocked = True
                    blocked_reason = "could not locate the eBay template serving this listing id"
                    row_out["blocked"] = True
                    row_out["blocked_reason"] = blocked_reason
        else:
            row_out["template_found"] = None
            row_out["staleness"] = None

        plan.append(row_out)

    verification_note = (
        "An 'unconfirmed' result is NOT evidence the listing changed — Linnworks' 204 "
        "response only means the push was ACCEPTED. Verify on the description-frame URL "
        f"({EBAY_DESCRIPTION_FRAME_URL_TEMPLATE.format(item_id='<item_id>')}), never the item "
        "page — the description sits in a cross-origin frame there and can report stale/old "
        f"text even when the new description is already live. Proven eBay accounts: "
        f"{_proven_ebay_revise_channels()}. {_ebay_push_observation_reason()} "
        f"{EBAY_UI_WORKAROUND_NOTE}"
    )

    base_out = {
        "sku_count": len(skus),
        "store": store,
        "revise_proven": EBAY_CHANNELS["ebay"]["revise_proven"],
        "push_observed_state": EBAY_CHANNELS["ebay"]["push_observed_state"],
        "verification_note": verification_note,
        "plan": plan,
        "not_listed": not_listed,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
        "complete": not rate_limited,
    }

    guard = _write_guard("revise_ebay_listing_description", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    pushable = [r for r in plan if not r["blocked"]]

    if dry_run:
        return {
            "dry_run": True, **base_out,
            "message": (
                f"{len(pushable)} listing(s) would be revised, {len(plan) - len(pushable)} "
                f"blocked, {len(not_listed)} not listed on '{store}', {len(unresolved)} "
                f"unresolved, {len(rate_limited)} rate-limited. No write calls were made. "
                + verification_note
            ),
        }

    results: list[dict] = []
    for row in plan:
        if row["blocked"]:
            outcome = (
                "rate_limited" if row.get("blocked_reason") == "rate_limited_locating_template"
                else "blocked"
            )
            results.append({**row, "outcome": outcome})
            continue
        template = template_by_listing.get(row["listing_id"])
        if template is None:
            results.append({
                **row, "outcome": "blocked",
                "error": "could not locate the eBay template serving this listing id",
            })
            continue
        payload = dict(template)
        payload["Description"] = row["description"]
        try:
            call_linnworks_void("Listings/ProcesseBayListings", {
                "items": [payload], "force": True, "action": "Update",
            })
        except RateLimitError as exc:
            rate_limited.append({"listing_id": row["listing_id"], "error": str(exc)})
            results.append({**row, "outcome": "rate_limited", "error": str(exc)})
            continue
        except RuntimeError as exc:
            results.append({**row, "outcome": "push_failed", "error": str(exc)})
            continue
        try:
            fresh = _find_ebay_template_for_listing(
                row["listing_id"], config_id=template.get("ConfigId"))
            post_push_read_back_rate_limited = False
        except RateLimitError as exc:
            rate_limited.append({"listing_id": row["listing_id"], "error": str(exc)})
            fresh = None
            post_push_read_back_rate_limited = True
        post_push_match = (
            fresh.get("Description") == row["description"]
            if isinstance(fresh, dict) else None
        )
        post_push_status = fresh.get("Status") if isinstance(fresh, dict) else None
        post_push_error_message = (
            fresh.get("ErrorMessage") if isinstance(fresh, dict) else None
        )
        results.append({
            **row, "outcome": "unconfirmed",
            "post_push_description_matches": post_push_match,
            "post_push_status": post_push_status,
            "post_push_error_message": post_push_error_message,
            "post_push_read_back_rate_limited": post_push_read_back_rate_limited,
        })

    base_out["rate_limited"] = rate_limited
    base_out["complete"] = not rate_limited
    unconfirmed_count = sum(1 for r in results if r["outcome"] == "unconfirmed")
    return {
        "dry_run": False, **base_out, "results": results,
        "message": (
            f"{unconfirmed_count} push(es) accepted (unconfirmed — acceptance is not "
            "evidence of change). " + verification_note
        ),
    }


SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-01")

_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

_GUID_PREFIX_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_|$)", re.I
)


def _linnworks_image_id(stem: str | None) -> str | None:
    if not stem:
        return None
    m = _GUID_PREFIX_RE.match(stem.strip())
    return m.group(1).lower() if m else None


def _shopify_store_for(sub_source: str) -> dict | None:
    want = (sub_source or "").strip().lower()

    raw = _env("SHOPIFY_STORES")
    if raw:
        try:
            stores = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"SHOPIFY_STORES is not valid JSON: {e}") from e
        for name, cfg in (stores or {}).items():
            if (name or "").strip().lower() == want:
                dom, tok = cfg.get("shop_domain"), cfg.get("access_token")
                if dom and tok:
                    return {"sub_source": name, "shop_domain": dom, "access_token": tok}

    default_ss = _env("SHOPIFY_DEFAULT_SUB_SOURCE") or DEFAULT_SHOPIFY_SUB_SOURCE
    dom = _env("SHOPIFY_SHOP_DOMAIN")
    tok = _env("SHOPIFY_ADMIN_ACCESS_TOKEN")
    if dom and tok and (default_ss or "").strip().lower() == want:
        return {"sub_source": default_ss, "shop_domain": dom, "access_token": tok}

    return None


def _shopify_setup_error(sub_source: str) -> dict:
    return {
        "error": (
            f"No Shopify Admin credentials configured for store '{sub_source}'. This tool "
            "writes to Shopify directly (the GLT cannot fix listing images), so it "
            "needs an Admin API access token that Linnworks does not provide."
        ),
        "how_to_fix": [
            "In Shopify admin: Settings > Apps and sales channels > Develop apps > Create an app.",
            "Grant Admin API scopes: read_products, write_products, read_files, write_files.",
            "Install the app and copy the Admin API access token (shpat_...).",
            "Add it to the linnworks MCP env — either single-store:",
            "    SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com",
            "    SHOPIFY_ADMIN_ACCESS_TOKEN=shpat_...",
            f"    SHOPIFY_DEFAULT_SUB_SOURCE={sub_source}",
            "  or multi-store (JSON, one entry per Linnworks SubSource):",
            '    SHOPIFY_STORES={"My Shopify Store": {"shop_domain": "...", "access_token": "shpat_..."}}',
            "Restart Claude Desktop so the server picks the new env up.",
        ],
        "shopify_configured": False,
    }


SHOPIFY_SCOPES_LISTING_CHECK = ("read_products",)
SHOPIFY_SCOPES_IMAGE_REPAIR = ("read_products", "write_products",
                               "read_files", "write_files")

_SHOPIFY_SCOPE_CACHE: dict[str, frozenset | None] = {}


def _shopify_granted_scopes(store: dict) -> frozenset | None:
    domain = store["shop_domain"]
    if domain in _SHOPIFY_SCOPE_CACHE:
        return _SHOPIFY_SCOPE_CACHE[domain]

    granted: frozenset | None = None
    try:
        resp = requests.get(
            f"https://{domain}/admin/oauth/access_scopes.json",
            headers={"X-Shopify-Access-Token": store["access_token"]},
            timeout=30,
        )
        if resp.status_code < 400:
            body = resp.json()
            scopes = body.get("access_scopes")
            if isinstance(scopes, list) and scopes:
                names = [a.get("handle") for a in scopes if isinstance(a, dict)]
                names = [n for n in names if n]
                if names:
                    granted = frozenset(names)
    except Exception:
        granted = None

    _SHOPIFY_SCOPE_CACHE[domain] = granted
    return granted


def _shopify_scope_error(sub_source: str, store: dict, needed, granted) -> dict:
    missing = [sc for sc in needed if sc not in granted]
    return {
        "error": (
            f"The Shopify Admin token for '{sub_source}' is installed and valid, but it "
            f"was not granted the scopes this tool needs. Missing: {', '.join(missing)}."
        ),
        "how_to_fix": [
            f"In Shopify admin for {store['shop_domain']}: Settings > Apps and sales "
            "channels > Develop apps > your app > Configuration.",
            f"Add the Admin API scopes: {', '.join(missing)}.",
            "Save, then re-install the app. Shopify issues a NEW access token when "
            "scopes change -- the old one keeps its original grant.",
            "Update SHOPIFY_ADMIN_ACCESS_TOKEN (or the SHOPIFY_STORES entry) with the "
            "new token, then restart Claude Desktop.",
            "The shop domain and the token itself are fine -- do not change them.",
        ],
        "shopify_configured": True,
        "scopes_ok": False,
        "scopes_needed": list(needed),
        "scopes_granted": sorted(granted),
        "scopes_missing": missing,
    }


def _shopify_scope_block(store: dict, sub_source: str, needed) -> dict | None:
    granted = _shopify_granted_scopes(store)
    if granted is None:
        return None
    if all(sc in granted for sc in needed):
        return None
    return _shopify_scope_error(sub_source, store, needed, granted)


def _shopify_graphql(store: dict, query: str, variables: dict) -> dict:
    url = f"https://{store['shop_domain']}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": store["access_token"],
        "Content-Type": "application/json",
    }

    for attempt, pause in enumerate([5, 10, 20, 30, None]):
        resp = requests.post(
            url, headers=headers, json={"query": query, "variables": variables}, timeout=60
        )

        throttled = resp.status_code == 429
        body: dict = {}
        if not throttled:
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Shopify Admin API HTTP {resp.status_code} on {store['shop_domain']}: "
                    f"{resp.text[:600]}"
                )
            try:
                body = resp.json()
            except ValueError as e:
                raise RuntimeError(f"Shopify returned non-JSON: {resp.text[:400]}") from e

            errors = body.get("errors") or []
            throttled = any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors
            )
            if errors and not throttled:
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(errors)[:600]}")

        if not throttled:
            return body.get("data") or {}

        if pause is None:
            raise RuntimeError(
                f"Shopify API still throttled after backoff ({store['shop_domain']}). "
                "Retry shortly — this is a quota pause, not a data problem."
            )
        time.sleep(pause)

    return {}


def _shopify_product_gid(channel_reference_id: str | None) -> str | None:
    ref = (channel_reference_id or "").strip()
    if not ref:
        return None
    head = ref.split(":")[0].strip()
    return f"gid://shopify/Product/{head}" if head.isdigit() else None


def _media_filename_stem(url: str | None) -> str | None:
    if not url:
        return None
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


_SHOPIFY_PROBE_NODES = """
query LwProbeListings($ids: [ID!]!) {
  nodes(ids: $ids) {
    __typename
    id
    ... on Product { title status }
  }
}
"""


def _shopify_listings_exist(store: dict, product_gids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    uniq = [g for g in dict.fromkeys(product_gids) if g]
    for i in range(0, len(uniq), 100):
        chunk = uniq[i:i + 100]
        data = _shopify_graphql(store, _SHOPIFY_PROBE_NODES, {"ids": chunk})
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(chunk):
            for gid in chunk:
                out[gid] = {"exists": None, "title": None, "status": None}
            continue
        for gid, node in zip(chunk, nodes):
            if isinstance(node, dict):
                out[gid] = {"exists": True, "title": node.get("title"),
                            "status": node.get("status")}
            else:
                out[gid] = {"exists": False, "title": None, "status": None}
    return out


def _glt_listing_existence(rows: list[dict], sub_source: str,
                           channel_source: str) -> dict:
    result = {"checked": False, "reason": None, "by_row": {}}

    if channel_source != GLT_SHOPIFY_CHANNEL_NAME:
        result["reason"] = (
            f"unsupported_channel — the listing-existence probe is Shopify-only; "
            f"{channel_source} listing ids have no cheap existence check here"
        )
        return result

    store = _shopify_store_for(sub_source)
    if store is None:
        result["reason"] = (
            "not_configured — no Shopify Admin credentials for this store, so a "
            "dangling ActiveListingId cannot be detected. See "
            "repair_channel_listing_images for the SHOPIFY_STORES / "
            "SHOPIFY_SHOP_DOMAIN setup."
        )
        return result

    scope_block = _shopify_scope_block(store, sub_source, SHOPIFY_SCOPES_LISTING_CHECK)
    if scope_block is not None:
        result["reason"] = (
            "insufficient_scope — the Shopify Admin token for this store is installed "
            "but was not granted %s, so a dangling ActiveListingId cannot be detected. "
            "Add the scope, re-install the app, and update the token (Shopify issues a "
            "new one when scopes change)." % ", ".join(scope_block["scopes_missing"])
        )
        return result

    gids: list[str] = []
    for r in rows:
        gid = _shopify_product_gid(r.get("active_listing_id"))
        if gid:
            gids.append(gid)

    if not gids:
        result["checked"] = True
        return result

    try:
        found = _shopify_listings_exist(store, gids)
    except RuntimeError as e:
        result["reason"] = f"check_failed — {e}"
        return result

    result["checked"] = True
    result["by_row"] = found
    return result

_SHOPIFY_READ_PRODUCT = """
query LwRepairReadProduct($id: ID!) {
  product(id: $id) {
    id
    title
    featuredMedia { id }
    media(first: 250) {
      nodes {
        id
        mediaContentType
        status
        mediaErrors { code details message }
        ... on MediaImage { image { url altText } }
      }
    }
  }
}
"""

_SHOPIFY_ADD_MEDIA = """
mutation LwRepairAddMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productUpdate(product: {id: $productId}, media: $media) {
    product { id }
    userErrors { field message }
  }
}
"""

_SHOPIFY_REORDER_MEDIA = """
mutation LwRepairSetFeatured($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id done }
    mediaUserErrors { field code message }
  }
}
"""

_SHOPIFY_DETACH_MEDIA = """
mutation LwRepairDetachMedia($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files { id fileStatus }
    userErrors { field code message }
  }
}
"""


def _shopify_read_media(store: dict, product_gid: str) -> dict:
    data = _shopify_graphql(store, _SHOPIFY_READ_PRODUCT, {"id": product_gid})
    product = data.get("product")
    if not product:
        return {"found": False, "product_gid": product_gid}

    nodes = ((product.get("media") or {}).get("nodes")) or []
    media = []
    for n in nodes:
        img = n.get("image") or {}
        url = img.get("url")
        stem = _media_filename_stem(url)
        media.append({
            "media_id":     n.get("id"),
            "status":       n.get("status"),
            "content_type": n.get("mediaContentType"),
            "url":          url,
            "stem":         stem,
            "lw_image_id":  _linnworks_image_id(stem),
            "alt":          img.get("altText"),
            "errors":       n.get("mediaErrors") or [],
        })

    featured = product.get("featuredMedia") or {}
    return {
        "found":             True,
        "product_gid":       product.get("id"),
        "product_title":     product.get("title"),
        "featured_media_id": featured.get("id"),
        "media":             media,
    }


def _shopify_poll_media(
    store: dict, product_gid: str, media_ids: list[str],
    timeout_s: int = 90, interval_s: int = 3,
) -> dict:
    wanted = {m for m in media_ids if m}
    deadline = time.time() + timeout_s
    seen: dict[str, dict] = {}

    while wanted and time.time() < deadline:
        time.sleep(interval_s)
        snapshot = _shopify_read_media(store, product_gid)
        for m in snapshot.get("media", []):
            mid = m["media_id"]
            if mid in wanted:
                seen[mid] = m
                if (m.get("status") or "").upper() != "PROCESSING":
                    wanted.discard(mid)

    return {"media": seen, "timed_out": sorted(wanted)}


def _owning_stock_item_ids(sku: str, stock_item_id: str) -> dict:
    try:
        rel = _resolve_variation(sku, stock_item_id)
    except RateLimitError:
        raise
    except Exception as e:
        return {
            "role": None, "group_name": None,
            "member_skus": [sku], "stock_item_ids": [stock_item_id],
            "shared_product": True,
            "lookup_error": f"Variation lookup failed ({e}) — treating the product as shared.",
        }

    role = rel.get("role")
    members = list(rel.get("children") or []) + list(rel.get("siblings") or [])
    ids = {(stock_item_id or "").lower()}
    skus = {sku}
    for m in members:
        if m.get("stock_item_id"):
            ids.add(m["stock_item_id"].lower())
        if m.get("sku"):
            skus.add(m["sku"])
    if rel.get("parent_stock_item_id"):
        ids.add(rel["parent_stock_item_id"].lower())
    if rel.get("parent_sku"):
        skus.add(rel["parent_sku"])

    return {
        "role":            role,
        "group_name":      rel.get("group_name"),
        "member_skus":     sorted(skus),
        "stock_item_ids":  sorted(ids),
        "shared_product":  role in ("parent", "child"),
    }


@_tool()
def repair_channel_listing_images(
    skus: list[str],
    sub_source: str = DEFAULT_SHOPIFY_SUB_SOURCE,
    remove_superseded: bool = True,
    set_featured: bool = True,
    allow_net_media_loss: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Put an item's CURRENT Linnworks images on its EXISTING Shopify listing. Use
    it when images were corrected in Linnworks but the product page still shows
    the old picture, is missing photos, or has the wrong hero image.
    refresh_channel_listing can't do this: it re-sends the template's stored image
    URL and reports success without changing anything. This tool calls the
    Shopify Admin API directly and checks the result on Shopify.

    Per SKU: read the Linnworks images, find the Shopify product through the
    channel-SKU reference, diff the two, ATTACH what is missing and wait for
    Shopify to finish processing it, make the Linnworks main image the featured
    image, then DETACH media that came from Linnworks but is no longer on the
    item. Images are matched by the Linnworks image id that Shopify keeps in the
    file name, so it compares actual pictures, not counts.

    What it will NOT do:
      - Remove media whose file name isn't a Linnworks image id (`unmanaged`):
        hand-uploaded photos, size charts, lifestyle shots.
      - Remove anything on a shared variation product unless every variant was
        passed in `skus`. All variants share one Shopify product but keep their
        own Linnworks images, so a sibling's photo would otherwise look
        superseded.
      - Remove anything before its replacement is READY, so a failed upload never
        leaves a listing without images.
      - Remove MORE media than it adds, unless allow_net_media_loss=True. That
        usually means the product page holds better content (e.g. lifestyle
        photos) than the catalogue; look at the images first.

    Needs Shopify Admin credentials with read_products, write_products,
    read_files and write_files. Without them it returns setup instructions and
    writes nothing. Batches over 10 SKUs need confirmed_count=len(skus).

    Args:
        skus: Exact SKUs to repair.
        sub_source: The Shopify store as named in Linnworks; must be a configured
            store. Defaults to LINNWORKS_DEFAULT_SHOPIFY_SUB_SOURCE.
        remove_superseded: Detach Linnworks media no longer on the item (default
            True). Detached files stay in Shopify Files, so this is recoverable.
        set_featured: Make the Linnworks main image the featured image (default
            True). Linnworks' is_main does not set this by itself.
        allow_net_media_loss: Allow removing more media than is added (default
            False).
        confirmed_count: For batches over 10, pass len(skus).
        dry_run: Default True: returns the plan and writes nothing.

    Returns:
        A dict with sub_source, shop_domain, dry_run, item_count;
        plan: per-SKU diff (missing, superseded, unmanaged and matched media,
            featured status, planned actions);
        unresolved: SKUs not repairable here, each with a `blocked_reason`;
        results (live run only): added, detached, featured, and `in_sync` from a
            fresh read of the product afterwards.
    """
    if not skus:
        raise ValueError("skus is empty — nothing to repair.")
    _check_injection("sub_source", sub_source)
    if not (sub_source or "").strip():
        return {
            "success": False,
            "error": "sub_source is required: name the Shopify store (Linnworks SubSource) "
                     "to repair, or set LINNWORKS_DEFAULT_SHOPIFY_SUB_SOURCE.",
            "skus": skus,
        }

    store = _shopify_store_for(sub_source)
    if store is None:
        return {**_shopify_setup_error(sub_source), "sub_source": sub_source, "skus": skus}

    blocked = _shopify_scope_block(store, sub_source, SHOPIFY_SCOPES_IMAGE_REPAIR)
    if blocked is not None:
        return {**blocked, "sub_source": sub_source, "skus": skus}

    lw_source = GLT_CHANNELS["shopify"]["source"]
    want_ss = (sub_source or "").strip().lower()

    plan: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []
    id_cache: dict = {}

    for sku in skus:
        try:
            sid = _resolve_sku_to_id(sku, id_cache)
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue
        except (ValueError, RuntimeError) as e:
            unresolved.append({"sku": sku, "blocked_reason": "not_found", "error": str(e)})
            continue

        try:
            rows = _fetch_channel_skus_for_ids([sid]).get(sid.lower(), [])
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue

        row = next(
            (r for r in rows
             if (r.get("Source") or "").upper() == lw_source
             and (r.get("SubSource") or "").strip().lower() == want_ss),
            None,
        )
        if row is None:
            unresolved.append({
                "sku": sku, "blocked_reason": "not_listed",
                "error": f"'{sku}' has no {lw_source} listing on '{sub_source}' — nothing to repair.",
            })
            continue

        product_gid = _shopify_product_gid(row.get("ChannelReferenceId"))
        if not product_gid:
            unresolved.append({
                "sku": sku, "blocked_reason": "no_product_reference",
                "channel_reference_id": row.get("ChannelReferenceId"),
                "error": (
                    "The channel-SKU row carries no usable Shopify product id "
                    f"(ChannelReferenceId={row.get('ChannelReferenceId')!r}). Expected a "
                    "'product:variant:inventory' triple."
                ),
            })
            continue

        try:
            own_raw = _fetch_raw_images(sid)
            owner = _owning_stock_item_ids(sku, sid)
            group_ids = owner["stock_item_ids"]
            group_imgs = _fetch_images_for_ids(group_ids) if len(group_ids) > 1 else {sid.lower(): own_raw}
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue

        own = [_format_image_row(r) for r in own_raw]
        own.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))
        own_ids = {(im["image_id"] or "").lower() for im in own if im.get("image_id")}
        group_owned_ids = {
            (img.get("pkRowId") or "").lower()
            for rows_ in group_imgs.values() for img in rows_ if img.get("pkRowId")
        } or own_ids

        if not own:
            unresolved.append({
                "sku": sku, "blocked_reason": "no_linnworks_images",
                "product_gid": product_gid,
                "error": (
                    f"'{sku}' has no images in Linnworks — there is nothing to push. Add one "
                    "with add_inventory_item_images first."
                ),
            })
            continue

        try:
            live = _shopify_read_media(store, product_gid)
        except RuntimeError as e:
            unresolved.append({
                "sku": sku, "blocked_reason": "shopify_read_failed",
                "product_gid": product_gid, "error": str(e),
            })
            continue

        if not live.get("found"):
            unresolved.append({
                "sku": sku, "blocked_reason": "shopify_product_missing",
                "product_gid": product_gid,
                "error": (
                    f"Shopify has no product {product_gid} — the Linnworks channel-SKU row is "
                    "stale. Nothing was changed."
                ),
            })
            continue

        by_stem = {m["lw_image_id"]: m for m in live["media"] if m.get("lw_image_id")}
        matched, superseded, unmanaged = [], [], []
        for m in live["media"]:
            stem = m.get("lw_image_id")
            if not stem:
                unmanaged.append(m)
                continue
            entry = {
                **m,
                "linnworks_image_id":  stem,
                "belongs_to_sibling":  stem not in own_ids and stem in group_owned_ids,
            }
            (matched if stem in group_owned_ids else superseded).append(entry)

        missing = [im for im in own if (im["image_id"] or "").lower() not in by_stem]

        main_img = next((im for im in own if im["is_main"]), own[0] if own else None)
        main_stem = (main_img["image_id"] or "").lower() if main_img else None
        main_media = by_stem.get(main_stem) if main_stem else None
        featured_ok = bool(main_media and main_media["media_id"] == live["featured_media_id"])

        requested = {s.strip().lower() for s in skus}
        all_members_requested = all(
            (s or "").strip().lower() in requested for s in owner.get("member_skus", [sku])
        )
        lookup_failed = bool(owner.get("lookup_error"))
        removal_allowed = remove_superseded and not lookup_failed and (
            not owner.get("shared_product") or all_members_requested
        )
        removal_blocked_reason = None
        if remove_superseded and not removal_allowed:
            removal_blocked_reason = owner.get("lookup_error") or (
                f"'{sku}' is a variation {owner.get('role') or 'member'} of "
                f"'{owner.get('group_name')}', whose Shopify product is shared with "
                f"{len(owner.get('member_skus', [])) - 1} other variant(s). Superseded media is "
                "NOT removed unless every member is passed in `skus`, because a sibling's photo "
                "lives on the same product."
            )

        net_loss = len(superseded) - len(missing)
        if removal_allowed and net_loss > 0 and not allow_net_media_loss:
            removal_allowed = False
            removal_blocked_reason = (
                f"Detaching would remove {len(superseded)} media while adding only "
                f"{len(missing)} — a net loss of {net_loss}. Linnworks is not always the "
                "richer image source (a storefront often carries lifestyle shots the "
                "catalogue never held), and a superseded image is indistinguishable from "
                "one that was simply removed from Linnworks. Review the images, then pass "
                "allow_net_media_loss=True if the removals really are stale versions."
            )

        to_detach = superseded if removal_allowed else []

        actions = []
        if missing:
            actions.append(f"attach {len(missing)} image(s)")
        if set_featured and main_media and not featured_ok:
            actions.append("set featured image")
        elif set_featured and not main_media and main_img:
            actions.append("set featured image (after attach)")
        if to_detach:
            actions.append(f"detach {len(to_detach)} superseded media")

        row_plan = {
            "sku":                  sku,
            "stock_item_id":        sid,
            "product_gid":          product_gid,
            "product_title":        live.get("product_title"),
            "channel_reference_id": row.get("ChannelReferenceId"),
            "linnworks_image_count": len(own),
            "shopify_media_count":  len(live["media"]),
            "missing":              missing,
            "matched":              matched,
            "superseded":           superseded,
            "unmanaged":            unmanaged,
            "to_detach":            to_detach,
            "main_image_id":        main_stem,
            "featured_media_id":    live["featured_media_id"],
            "featured_is_correct":  featured_ok,
            "variation":            {
                "role":           owner.get("role"),
                "group_name":     owner.get("group_name"),
                "shared_product": owner.get("shared_product"),
                "member_skus":    owner.get("member_skus"),
            },
            "removal_blocked_reason": removal_blocked_reason,
            "actions":            actions,
            "needs_repair":       bool(actions),
        }
        plan.append(row_plan)

    actionable = [p for p in plan if p["needs_repair"]]

    base = {
        "sub_source":          sub_source,
        "shop_domain":         store["shop_domain"],
        "shopify_configured":  True,
        "api_version":         SHOPIFY_API_VERSION,
        "item_count":          len(skus),
        "plan":                plan,
        "unresolved":          unresolved,
        "rate_limited":        rate_limited,
        "complete":            not rate_limited,
        "repairable_count":    len(actionable),
        "in_sync_count":       len(plan) - len(actionable),
        "blocked_count":       len(unresolved),
    }

    guard = _write_guard("repair_channel_listing_images", actionable, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base}

    if not actionable:
        return {
            **base, "dry_run": dry_run, "results": [],
            "message": (
                f"Nothing to repair — {len(plan)} listing(s) already match their Linnworks images"
                + (f"; {len(unresolved)} SKU(s) blocked, see `unresolved`." if unresolved else ".")
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "results": [],
            "message": (
                f"Dry run — nothing written. {len(actionable)} of {len(plan)} listing(s) would be "
                f"repaired on '{sub_source}'. Set dry_run=False to execute."
            ),
        }

    results = []
    for p in actionable:
        pgid = p["product_gid"]
        out = {
            "sku": p["sku"], "product_gid": pgid, "product_title": p["product_title"],
            "added": [], "add_failed": [], "detached": [], "detach_failed": [],
            "featured_set": None, "errors": [],
        }

        try:
            new_ids: list[str] = []
            if p["missing"]:
                before = {m["media_id"] for m in _shopify_read_media(store, pgid)["media"]}
                media_input = [{
                    "originalSource":   im["full_url"],
                    "mediaContentType": "IMAGE",
                    "alt":              p["product_title"] or p["sku"],
                } for im in p["missing"] if im.get("full_url")]

                data = _shopify_graphql(
                    store, _SHOPIFY_ADD_MEDIA, {"productId": pgid, "media": media_input}
                )
                errs = ((data.get("productUpdate") or {}).get("userErrors")) or []
                if errs:
                    out["errors"].append({"stage": "add", "user_errors": errs})

                after = _shopify_read_media(store, pgid)["media"]
                new_ids = [m["media_id"] for m in after if m["media_id"] not in before]

                polled = _shopify_poll_media(store, pgid, new_ids)
                for mid in new_ids:
                    m = polled["media"].get(mid, {})
                    status = (m.get("status") or "UNKNOWN").upper()
                    rec = {"media_id": mid, "status": status, "url": m.get("url"),
                           "linnworks_image_id": m.get("lw_image_id"),
                           "errors": m.get("errors") or []}
                    (out["added"] if status == "READY" else out["add_failed"]).append(rec)
                if polled["timed_out"]:
                    out["errors"].append({
                        "stage": "add",
                        "still_processing": polled["timed_out"],
                        "note": "Shopify was still processing these when the poll timed out.",
                    })

            live = _shopify_read_media(store, pgid)
            by_stem = {m["lw_image_id"]: m for m in live["media"] if m.get("lw_image_id")}

            if set_featured and p["main_image_id"]:
                main_media = by_stem.get(p["main_image_id"])
                if main_media and main_media["media_id"] != live["featured_media_id"]:
                    d = _shopify_graphql(store, _SHOPIFY_REORDER_MEDIA, {
                        "id": pgid,
                        "moves": [{"id": main_media["media_id"], "newPosition": "0"}],
                    })
                    merrs = ((d.get("productReorderMedia") or {}).get("mediaUserErrors")) or []
                    if merrs:
                        out["errors"].append({"stage": "featured", "user_errors": merrs})
                    else:
                        out["featured_set"] = main_media["media_id"]
                elif main_media:
                    out["featured_set"] = "already_correct"

            if p["to_detach"]:
                if p["missing"] and not out["added"]:
                    out["errors"].append({
                        "stage": "detach",
                        "skipped": True,
                        "note": (
                            "No replacement image reached READY, so superseded media was left in "
                            "place rather than emptying the listing."
                        ),
                    })
                else:
                    files = [{"id": m["media_id"], "referencesToRemove": [pgid]}
                             for m in p["to_detach"]]
                    d = _shopify_graphql(store, _SHOPIFY_DETACH_MEDIA, {"files": files})
                    uerrs = ((d.get("fileUpdate") or {}).get("userErrors")) or []
                    if uerrs:
                        out["errors"].append({"stage": "detach", "user_errors": uerrs})

            final = _shopify_read_media(store, pgid)
            final_stems = {m["lw_image_id"] for m in final["media"] if m.get("lw_image_id")}
            lw_ids = {(im["image_id"] or "").lower()
                      for im in p["missing"]} | {
                      (m.get("linnworks_image_id") or "") for m in p["matched"]}
            lw_ids.discard("")

            detached_ids = {m["media_id"] for m in p["to_detach"]}
            remaining_ids = {m["media_id"] for m in final["media"]}
            out["detached"] = sorted(detached_ids - remaining_ids)
            out["detach_failed"] = sorted(detached_ids & remaining_ids)

            still_missing = sorted(i for i in lw_ids if i not in final_stems)
            final_by_stem = {m["lw_image_id"]: m
                             for m in final["media"] if m.get("lw_image_id")}
            main_media_final = final_by_stem.get(p["main_image_id"]) if p["main_image_id"] else None
            featured_now = final["featured_media_id"]
            featured_ok = bool(
                main_media_final and featured_now == main_media_final["media_id"]
            ) if (set_featured and p["main_image_id"]) else None

            out["still_missing"] = still_missing
            out["featured_media_id"] = featured_now
            out["featured_is_correct"] = featured_ok
            out["shopify_media_count"] = len(final["media"])
            out["in_sync"] = (
                not still_missing
                and not out["detach_failed"]
                and not out["add_failed"]
                and (featured_ok is not False)
            )
        except RuntimeError as e:
            out["errors"].append({"stage": "unhandled", "error": str(e)})
            out["in_sync"] = None

        results.append(out)

    repaired = sum(1 for r in results if r.get("in_sync") is True)
    failed = [r["sku"] for r in results if r.get("in_sync") is not True]

    return {
        **base, "dry_run": False, "results": results,
        "repaired_count": repaired,
        "failed_skus": failed,
        "message": (
            f"Repaired {repaired} of {len(results)} listing(s) on '{sub_source}'; verified by "
            "re-reading each product from Shopify."
            + (f" ⚠️ Not confirmed in sync: {failed} — read `results[].errors`." if failed else "")
        ),
    }


def _assert_every_classified_tool_is_registered() -> None:
    import asyncio

    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    classified = _READ_ONLY_TOOLS | _ADDITIVE_WRITE_TOOLS | _DESTRUCTIVE_WRITE_TOOLS
    stale = sorted(classified - registered)
    if stale:
        raise RuntimeError(f"Tool classification names unregistered tools: {stale}")


_assert_every_classified_tool_is_registered()


def main() -> None:
    if "--list-tools" in sys.argv:
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        for t in sorted(tools, key=lambda x: x.name):
            print(t.name)
        print(f"\n{len(tools)} tools registered")
        sys.exit(0)

    if "--check-auth" in sys.argv:
        _require_credentials()
        try:
            token, server = authorize()
            masked = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "<short>"
            print(f"Auth OK")
            print(f"  Server: {server}")
            print(f"  Token:  {masked}")
            sys.exit(0)
        except Exception as e:
            print(f"Auth failed: {e}", file=sys.stderr)
            sys.exit(1)

    _require_credentials()
    mcp.run()


if __name__ == "__main__":
    main()
