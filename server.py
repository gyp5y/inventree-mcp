#!/usr/bin/env python3
"""InvenTree MCP Server — 12 parameterized tools, 117 operations.

Dual transport: STDIO (default) or SSE (pass 'sse' argument).
Uses inventree-python library wrapped in asyncio.to_thread() for async safety.
"""

import asyncio
import json
import logging
import os
import sys
from typing import Any

import anyio
from dotenv import load_dotenv
from mcp.server.stdio import stdio_server
from mcp.server.fastmcp import FastMCP
from mcp.shared.message import SessionMessage
from mcp.types import (
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCRequest,
    LATEST_PROTOCOL_VERSION,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("inventree_mcp")


async def _handle_stdio_discover(message: SessionMessage, write_stream) -> bool:
    """Make modern clients fall back cleanly when this SDK is legacy-only."""
    root = message.message.root
    if not isinstance(root, JSONRPCRequest) or root.method != "server/discover":
        return False
    if LATEST_PROTOCOL_VERSION >= "2026-07-28":
        return False

    response = JSONRPCError(
        jsonrpc="2.0",
        id=root.id,
        error=ErrorData(code=-32601, message="Method not found"),
    )
    await write_stream.send(SessionMessage(message=JSONRPCMessage(response)))
    return True


async def run_stdio_with_discovery_async() -> None:
    """Run stdio transport with a compatibility shim for server/discover."""
    async with stdio_server() as (read_stream, write_stream):
        filtered_writer, filtered_read = anyio.create_memory_object_stream(0)

        async def filter_discovery_requests() -> None:
            async with filtered_writer:
                async for message in read_stream:
                    if isinstance(message, Exception):
                        await filtered_writer.send(message)
                    elif not await _handle_stdio_discover(message, write_stream):
                        await filtered_writer.send(message)

        async with anyio.create_task_group() as tg:
            tg.start_soon(filter_discovery_requests)
            await mcp._mcp_server.run(
                filtered_read,
                write_stream,
                mcp._mcp_server.create_initialization_options(),
            )
            tg.cancel_scope.cancel()

# ── Lazy client singleton ────────────────────────────────────────────

_client = None
_client_lock = asyncio.Lock()


async def get_client():
    """Get or create the InvenTree client singleton."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        from client import InvenTreeClient

        url = os.getenv("INVENTREE_URL")
        token = os.getenv("INVENTREE_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "INVENTREE_URL and INVENTREE_TOKEN must be set in environment"
            )
        _client = InvenTreeClient(url, token)
        await _client.connect()
        logger.info(f"InvenTree client connected to {url}")
    return _client


def _json(data: Any) -> str:
    """Serialize response data to JSON string."""
    return json.dumps(data, indent=2, default=str)


def _error(e: Exception, context: str = "") -> str:
    """Format an actionable error message for the agent."""
    msg = str(e)
    if "404" in msg or "not found" in msg.lower():
        hint = "Check that the ID exists. Use the list/search operation first to find valid IDs."
    elif "403" in msg or "permission" in msg.lower() or "forbidden" in msg.lower():
        hint = "The API token lacks permission for this operation. Check INVENTREE_TOKEN roles."
    elif "400" in msg or "bad request" in msg.lower():
        hint = "Invalid request data. Check required fields in the 'data' parameter."
    elif "timeout" in msg.lower() or "connect" in msg.lower():
        hint = "Connection failed. Verify INVENTREE_URL is reachable."
    elif "INVENTREE_URL" in msg or "INVENTREE_TOKEN" in msg:
        hint = "Set INVENTREE_URL and INVENTREE_TOKEN environment variables."
    else:
        hint = "Check the operation name and parameters."
    prefix = f"[{context}] " if context else ""
    logger.error(f"{prefix}{msg}")
    return _json({"error": f"{prefix}{msg}", "hint": hint})


def _safe(tool_name: str):
    """Decorator to add error handling to tool functions."""
    import functools

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as e:
                op = kwargs.get("operation", args[0] if args else "unknown")
                return _error(e, f"{tool_name}.{op}")
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════════════
# Tool Definitions — 12 parameterized tools
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    annotations={
        "title": "InvenTree Part & Category Management",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("part")
async def part(
    operation: str,
    pk: int = None,
    data: dict = None,
    search: str = None,
    category: int = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Part & category management in InvenTree.

    Operations:
      Read: list, get, get_stock, get_bom, get_bom_usage, get_suppliers,
        get_manufacturers, get_parameters, get_parameter_templates,
        get_test_templates, get_related, get_builds, get_internal_prices,
        get_sale_prices, list_categories, get_category, get_category_parameters
      Write: create, update, upload_image, create_category, create_parameter_template,
        update_parameter_template, create_parameter, update_parameter,
        upsert_parameters, delete_parameter
      Delete: delete

    Args:
        operation: One of the operations listed above.
        pk: Part or category ID (required for get/update/delete and sub-queries).
        data: Dict of fields for create/update. upload_image uses pk=part ID and\n          data={"image_base64": "...", "filename": "photo.jpg", "replace": false}\n          or data={"file_path": "/path/on/mcp/server/photo.jpg", "replace": false}.\n          Parameter operations use pk=part ID;
          create_parameter: {"template": 12, "data": "60 V", "note": "..."};
          update/delete_parameter: {"parameter_id": 34, "data": "..." };
          upsert_parameters: {"parameters": [{"template": 12, "data": "60 V"}]}.
          Template operations use pk=template ID for update.
        search: Text search filter for list/list_categories.
        category: Category ID filter for list.
        limit: Max results for list (default 25).
        offset: Pagination offset for list.

    Returns:
        JSON string with part/category data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {}
        if search:
            filters["search"] = search
        if category is not None:
            filters["category"] = category
        filters["limit"] = limit
        filters["offset"] = offset
        return _json(await c.part_list(**filters))

    elif operation == "get":
        return _json(await c.part_get(pk))

    elif operation == "create":
        return _json(await c.part_create(data or {}))

    elif operation == "update":
        return _json(await c.part_update(pk, data or {}))

    elif operation == "delete":
        return _json(await c.part_delete(pk))

    elif operation == "upload_image":
        return _json(await c.part_upload_image(
            pk, image_base64=(data or {}).get("image_base64"),
            file_path=(data or {}).get("file_path"),
            filename=(data or {}).get("filename", "chat-photo.jpg"),
            replace=(data or {}).get("replace", False)))

    elif operation == "get_stock":
        return _json(await c.part_get_stock(pk))

    elif operation == "get_bom":
        return _json(await c.part_get_bom(pk))

    elif operation == "get_bom_usage":
        return _json(await c.part_get_bom_usage(pk))

    elif operation == "get_suppliers":
        return _json(await c.part_get_suppliers(pk))

    elif operation == "get_manufacturers":
        return _json(await c.part_get_manufacturers(pk))

    elif operation == "get_parameters":
        return _json(await c.part_get_parameters(pk))

    elif operation == "get_parameter_templates":
        return _json(await c.part_get_parameter_templates())

    elif operation == "create_parameter_template":
        return _json(await c.part_create_parameter_template(data or {}))

    elif operation == "update_parameter_template":
        return _json(await c.part_update_parameter_template(pk, data or {}))

    elif operation == "create_parameter":
        return _json(await c.part_create_parameter(pk, data or {}))

    elif operation == "update_parameter":
        if not isinstance(data, dict) or "parameter_id" not in data:
            raise ValueError("data.parameter_id required")
        return _json(await c.part_update_parameter(pk, data["parameter_id"],
            {k: v for k, v in data.items() if k != "parameter_id"}))

    elif operation == "delete_parameter":
        if not isinstance(data, dict) or "parameter_id" not in data:
            raise ValueError("data.parameter_id required")
        return _json(await c.part_delete_parameter(pk, data["parameter_id"]))

    elif operation == "upsert_parameters":
        if not isinstance(data, dict) or not isinstance(data.get("parameters"), list):
            raise ValueError('data must contain "parameters" list')
        return _json(await c.part_upsert_parameters(pk, data["parameters"]))

    elif operation == "get_test_templates":
        return _json(await c.part_get_test_templates(pk))

    elif operation == "get_related":
        return _json(await c.part_get_related(pk))

    elif operation == "get_builds":
        return _json(await c.part_get_builds(pk))

    elif operation == "get_internal_prices":
        return _json(await c.part_get_internal_prices(pk))

    elif operation == "get_sale_prices":
        return _json(await c.part_get_sale_prices(pk))

    elif operation == "list_categories":
        filters = {}
        if search:
            filters["search"] = search
        return _json(await c.part_list_categories(**filters))

    elif operation == "get_category":
        return _json(await c.part_get_category(pk))

    elif operation == "create_category":
        return _json(await c.part_create_category(data or {}))

    elif operation == "get_category_parameters":
        return _json(await c.part_get_category_parameters(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Stock & Location Management",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("stock")
async def stock(
    operation: str,
    pk: int = None,
    data: dict = None,
    part_id: int = None,
    location_id: int = None,
    quantity: float = None,
    test_name: str = None,
    test_result: bool = None,
    search: str = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Stock item & location management in InvenTree.

    Operations:
      Read: list, get, get_by_location, get_by_part, list_locations,
        get_location, get_tracking, get_test_results
      Write: create, update, transfer, count, add, remove, create_location,
        upload_test_result

    Args:
        operation: One of the operations listed above.
        pk: Stock item or location ID.
        data: Dict of fields for create/update.
        part_id: Filter stock by part (for list, get_by_part).
        location_id: Filter stock by location or transfer destination.
        quantity: Quantity for transfer/count/add/remove operations.
        test_name: Name for upload_test_result.
        test_result: Boolean pass/fail for upload_test_result.
        search: Text search filter for list/list_locations.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with stock/location data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        if part_id is not None:
            filters["part"] = part_id
        if location_id is not None:
            filters["location"] = location_id
        return _json(await c.stock_list(**filters))

    elif operation == "get":
        return _json(await c.stock_get(pk))

    elif operation == "create":
        return _json(await c.stock_create(data or {}))

    elif operation == "update":
        return _json(await c.stock_update(pk, data or {}))

    elif operation == "transfer":
        return _json(await c.stock_transfer(pk, location_id, quantity))

    elif operation == "count":
        return _json(await c.stock_count(pk, quantity))

    elif operation == "add":
        return _json(await c.stock_add(pk, quantity))

    elif operation == "remove":
        return _json(await c.stock_remove(pk, quantity))

    elif operation == "get_by_location":
        return _json(await c.stock_get_by_location(location_id))

    elif operation == "get_by_part":
        return _json(await c.stock_get_by_part(part_id))

    elif operation == "list_locations":
        filters = {}
        if search:
            filters["search"] = search
        return _json(await c.stock_list_locations(**filters))

    elif operation == "get_location":
        return _json(await c.stock_get_location(pk))

    elif operation == "create_location":
        return _json(await c.stock_create_location(data or {}))

    elif operation == "get_tracking":
        return _json(await c.stock_get_tracking(pk))

    elif operation == "upload_test_result":
        extra = {}
        if "notes" in (data or {}):
            extra["notes"] = data["notes"]
        if "value" in (data or {}):
            extra["value"] = data["value"]
        return _json(await c.stock_upload_test_result(pk, test_name, test_result, **extra))

    elif operation == "get_test_results":
        return _json(await c.stock_get_test_results(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Build Order Management",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("build_order")
async def build_order(
    operation: str,
    pk: int = None,
    data: dict = None,
    items: list = None,
    search: str = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Manufacturing build order management in InvenTree.

    Operations:
      Read: list, get, get_outputs, get_lines
      Write: create, update, allocate, complete, cancel

    Args:
        operation: One of the operations listed above.
        pk: Build order ID.
        data: Dict of fields for create/update/complete.
        items: List of allocation dicts for allocate (e.g. [{"stock_item": 1, "quantity": 5, "build_line": 3}]).
        search: Text search filter for list.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with build order data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        return _json(await c.build_list(**filters))

    elif operation == "get":
        return _json(await c.build_get(pk))

    elif operation == "create":
        return _json(await c.build_create(data or {}))

    elif operation == "update":
        return _json(await c.build_update(pk, data or {}))

    elif operation == "allocate":
        return _json(await c.build_allocate(pk, items or []))

    elif operation == "complete":
        return _json(await c.build_complete(pk, **(data or {})))

    elif operation == "cancel":
        return _json(await c.build_cancel(pk))

    elif operation == "get_outputs":
        return _json(await c.build_get_outputs(pk))

    elif operation == "get_lines":
        return _json(await c.build_get_lines(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Purchase Order Management",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("purchase_order")
async def purchase_order(
    operation: str,
    pk: int = None,
    data: dict = None,
    location_id: int = None,
    items: list = None,
    search: str = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Purchase order lifecycle management in InvenTree.

    Operations:
      Read: list, get, get_line_items
      Write: create, update, issue, receive, complete, cancel, hold,
        add_line_item, add_extra_line_item

    Args:
        operation: One of the operations listed above.
        pk: Purchase order ID.
        data: Dict of fields for create/update/add_line_item.
        location_id: Receiving location for receive operation.
        items: Line items to receive (e.g. [{"line_item": 1, "quantity": 10}]).
        search: Text search filter for list.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with PO data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        return _json(await c.po_list(**filters))

    elif operation == "get":
        return _json(await c.po_get(pk))

    elif operation == "create":
        return _json(await c.po_create(data or {}))

    elif operation == "update":
        return _json(await c.po_update(pk, data or {}))

    elif operation == "issue":
        return _json(await c.po_issue(pk))

    elif operation == "receive":
        return _json(await c.po_receive(pk, location_id, items))

    elif operation == "complete":
        return _json(await c.po_complete(pk))

    elif operation == "cancel":
        return _json(await c.po_cancel(pk))

    elif operation == "hold":
        return _json(await c.po_hold(pk))

    elif operation == "add_line_item":
        return _json(await c.po_add_line_item(pk, data or {}))

    elif operation == "add_extra_line_item":
        return _json(await c.po_add_extra_line_item(pk, data or {}))

    elif operation == "get_line_items":
        return _json(await c.po_get_line_items(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Sales Order Management",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("sales_order")
async def sales_order(
    operation: str,
    pk: int = None,
    data: dict = None,
    shipment_id: int = None,
    reference: str = None,
    search: str = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Sales order lifecycle management in InvenTree.

    Operations:
      Read: list, get, get_line_items, get_allocations
      Write: create, update, complete, cancel, hold, add_line_item,
        add_extra_line_item, create_shipment, complete_shipment

    Args:
        operation: One of the operations listed above.
        pk: Sales order ID.
        data: Dict of fields for create/update/add_line_item.
        shipment_id: Shipment ID for complete_shipment.
        reference: Reference string for create_shipment.
        search: Text search filter for list.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with SO data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        return _json(await c.so_list(**filters))

    elif operation == "get":
        return _json(await c.so_get(pk))

    elif operation == "create":
        return _json(await c.so_create(data or {}))

    elif operation == "update":
        return _json(await c.so_update(pk, data or {}))

    elif operation == "complete":
        return _json(await c.so_complete(pk))

    elif operation == "cancel":
        return _json(await c.so_cancel(pk))

    elif operation == "hold":
        return _json(await c.so_hold(pk))

    elif operation == "add_line_item":
        return _json(await c.so_add_line_item(pk, data or {}))

    elif operation == "add_extra_line_item":
        return _json(await c.so_add_extra_line_item(pk, data or {}))

    elif operation == "get_line_items":
        return _json(await c.so_get_line_items(pk))

    elif operation == "create_shipment":
        return _json(await c.so_create_shipment(pk, reference, **(data or {})))

    elif operation == "complete_shipment":
        return _json(await c.so_complete_shipment(shipment_id, **(data or {})))

    elif operation == "get_allocations":
        return _json(await c.so_get_allocations(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Return Order Management",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("return_order")
async def return_order(
    operation: str,
    pk: int = None,
    data: dict = None,
    search: str = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Return order lifecycle management in InvenTree.

    Operations:
      Read: list, get, get_line_items
      Write: create, update, complete, cancel, add_line_item

    Args:
        operation: One of the operations listed above.
        pk: Return order ID.
        data: Dict of fields for create/update/add_line_item.
        search: Text search filter for list.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with RO data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        return _json(await c.ro_list(**filters))

    elif operation == "get":
        return _json(await c.ro_get(pk))

    elif operation == "create":
        return _json(await c.ro_create(data or {}))

    elif operation == "update":
        return _json(await c.ro_update(pk, data or {}))

    elif operation == "complete":
        return _json(await c.ro_complete(pk))

    elif operation == "cancel":
        return _json(await c.ro_cancel(pk))

    elif operation == "add_line_item":
        return _json(await c.ro_add_line_item(pk, data or {}))

    elif operation == "get_line_items":
        return _json(await c.ro_get_line_items(pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Company Management",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("company")
async def company(
    operation: str,
    pk: int = None,
    data: dict = None,
    company_id: int = None,
    supplier_part_id: int = None,
    search: str = None,
    is_supplier: bool = None,
    is_manufacturer: bool = None,
    is_customer: bool = None,
    limit: int = 25,
    offset: int = 0,
) -> str:
    """Supplier, manufacturer, and customer management in InvenTree.

    Operations:
      Read: list, get, list_supplier_parts, get_supplier_part,
        list_manufacturer_parts, get_manufacturer_part, list_contacts,
        list_addresses, get_price_breaks
      Write: create, update
      Delete: delete

    Args:
        operation: One of the operations listed above.
        pk: Company, supplier part, or manufacturer part ID.
        data: Dict of fields for create/update.
        company_id: Company ID for list_supplier_parts/list_manufacturer_parts/list_contacts/list_addresses.
        supplier_part_id: Supplier part ID for get_price_breaks.
        search: Text search filter for list/list_supplier_parts/list_manufacturer_parts.
        is_supplier/is_manufacturer/is_customer: Boolean filters for list.
        limit: Max results (default 25).
        offset: Pagination offset.

    Returns:
        JSON string with company data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        filters = {"limit": limit, "offset": offset}
        if search:
            filters["search"] = search
        if is_supplier is not None:
            filters["is_supplier"] = is_supplier
        if is_manufacturer is not None:
            filters["is_manufacturer"] = is_manufacturer
        if is_customer is not None:
            filters["is_customer"] = is_customer
        return _json(await c.company_list(**filters))

    elif operation == "get":
        return _json(await c.company_get(pk))

    elif operation == "create":
        return _json(await c.company_create(data or {}))

    elif operation == "update":
        return _json(await c.company_update(pk, data or {}))

    elif operation == "delete":
        return _json(await c.company_delete(pk))

    elif operation == "list_supplier_parts":
        filters = {}
        if company_id is not None:
            filters["supplier"] = company_id
        if search:
            filters["search"] = search
        return _json(await c.company_list_supplier_parts(**filters))

    elif operation == "get_supplier_part":
        return _json(await c.company_get_supplier_part(pk))

    elif operation == "list_manufacturer_parts":
        filters = {}
        if company_id is not None:
            filters["manufacturer"] = company_id
        if search:
            filters["search"] = search
        return _json(await c.company_list_manufacturer_parts(**filters))

    elif operation == "get_manufacturer_part":
        return _json(await c.company_get_manufacturer_part(pk))

    elif operation == "list_contacts":
        return _json(await c.company_list_contacts(company_id or pk))

    elif operation == "list_addresses":
        return _json(await c.company_list_addresses(company_id or pk))

    elif operation == "get_price_breaks":
        return _json(await c.company_get_price_breaks(supplier_part_id or pk))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Barcode Operations",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("barcode")
async def barcode(
    operation: str,
    barcode_data: str = None,
    model_type: str = None,
    pk: int = None,
) -> str:
    """Barcode scanning and assignment in InvenTree.

    Operations:
      Read: scan, lookup
      Write: assign, unassign

    Args:
        operation: One of the operations listed above.
        barcode_data: Barcode string for scan/assign/lookup.
        model_type: Model type for assign/unassign (part, stockitem, stocklocation, build, purchaseorder, salesorder, returnorder).
        pk: Object ID for assign/unassign.

    Returns:
        JSON string with barcode result or {"error": "..."}.
    """
    c = await get_client()

    if operation == "scan":
        return _json(await c.barcode_scan(barcode_data))

    elif operation == "assign":
        return _json(await c.barcode_assign(model_type, pk, barcode_data))

    elif operation == "unassign":
        return _json(await c.barcode_unassign(model_type, pk))

    elif operation == "lookup":
        return _json(await c.barcode_lookup(barcode_data))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Label Printing",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("label")
async def label(
    operation: str,
    label_type: str = "part",
    template_id: int = None,
    item_ids: list = None,
    destination: str = None,
) -> str:
    """Label printing and template management in InvenTree.

    Operations: list_templates, print_part, print_stock, print_location, download_template

    Args:
        operation: One of the operations listed above.
        label_type: Filter for list_templates (part, stock, location).
        template_id: Label template ID for print/download operations.
        item_ids: List of item IDs to print labels for.
        destination: File path to save downloaded template to.

    Returns:
        JSON string with label templates or print result.
    """
    c = await get_client()

    if operation == "list_templates":
        return _json(await c.label_list_templates(label_type))

    elif operation in ("print_part", "print_stock", "print_location"):
        lt = operation.replace("print_", "")
        return _json(await c.label_print(lt, template_id, item_ids or []))

    elif operation == "download_template":
        return _json(await c.label_download_template(template_id, destination))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Report Generation",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("report")
async def report(
    operation: str,
    template_id: int = None,
    item_ids: list = None,
    model_type: str = None,
    destination: str = None,
) -> str:
    """Report generation and template management in InvenTree.

    Operations: list_templates, print_report, download_template

    Args:
        operation: One of the operations listed above.
        template_id: Report template ID for print_report/download_template.
        item_ids: List of item IDs to include in report.
        model_type: Model type for print_report.
        destination: File path to save downloaded template to.

    Returns:
        JSON string with report templates or print result.
    """
    c = await get_client()

    if operation == "list_templates":
        return _json(await c.report_list_templates())

    elif operation == "print_report":
        return _json(await c.report_print(template_id, item_ids or [], model_type))

    elif operation == "download_template":
        return _json(await c.report_download_template(template_id, destination))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree Attachment Management",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
@_safe("attachment")
async def attachment(
    operation: str,
    model_type: str = None,
    model_id: int = None,
    attachment_id: int = None,
    file_path: str = None,
    link: str = None,
    comment: str = "",
    destination: str = None,
    image_base64: str = None,
    filename: str = "chat-photo.jpg",
) -> str:
    """File attachment management for any InvenTree object.

    Operations:
      Read: list, download
      Write: upload, upload_link, upload_image
      Delete: delete

    Args:
        operation: One of the operations listed above.
        model_type: Object type (part, stockitem, build, purchaseorder, salesorder, company).
        model_id: Object ID for list/upload/upload_link.
        attachment_id: Attachment ID for download/delete.
        file_path: Local file path on the MCP server for upload.\n        image_base64: Base64 image data or data URL for upload_image.\n        filename: Image filename for upload_image.\n        link: URL for upload_link.
        comment: Optional comment for uploads.
        destination: File path to save downloaded attachment to.

    Returns:
        JSON string with attachment data or {"error": "..."}.
    """
    c = await get_client()

    if operation == "list":
        return _json(await c.attachment_list(model_type, model_id))

    elif operation == "upload":
        return _json(await c.attachment_upload(model_type, model_id, file_path, comment))

    elif operation == "upload_link":
        return _json(await c.attachment_upload_link(model_type, model_id, link, comment))

    elif operation == "upload_image":
        return _json(await c.attachment_upload_image(
            model_type, model_id, image_base64=image_base64,
            filename=filename, comment=comment))

    elif operation == "download":
        return _json(await c.attachment_download(attachment_id, destination))

    elif operation == "delete":
        return _json(await c.attachment_delete(attachment_id))

    else:
        return _json({"error": f"Unknown operation: {operation}"})


@mcp.tool(
    annotations={
        "title": "InvenTree System Administration",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
@_safe("system")
async def system(
    operation: str,
    pk: int = None,
) -> str:
    """InvenTree system administration and health checks.

    Operations: health, version, settings, list_users, get_user,
    list_groups, list_owners, get_project_codes, list_currencies

    All operations are read-only.

    Args:
        operation: One of the operations listed above.
        pk: User ID for get_user.

    Returns:
        JSON string with system data.
    """
    c = await get_client()

    if operation == "health":
        return _json(await c.system_health())

    elif operation == "version":
        return _json(await c.system_version())

    elif operation == "settings":
        return _json(await c.system_settings())

    elif operation == "list_users":
        return _json(await c.system_list_users())

    elif operation == "get_user":
        return _json(await c.system_get_user(pk))

    elif operation == "list_groups":
        return _json(await c.system_list_groups())

    elif operation == "list_owners":
        return _json(await c.system_list_owners())

    elif operation == "get_project_codes":
        return _json(await c.system_get_project_codes())

    elif operation == "list_currencies":
        return _json(await c.system_list_currencies())

    else:
        return _json({"error": f"Unknown operation: {operation}"})


# ═══════════════════════════════════════════════════════════════════════
# Entry point — dual transport
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"

    if transport == "sse":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.getenv("PORT", "3074"))
        logger.info(f"Starting InvenTree MCP Server on SSE port {port}")
        mcp.run(transport="sse", port=port)
    else:
        logger.info("Starting InvenTree MCP Server on STDIO")
        anyio.run(run_stdio_with_discovery_async)
