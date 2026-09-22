"""Async adapter wrapping the sync inventree-python library.

All sync calls are offloaded to threads via asyncio.to_thread() with a
semaphore to limit concurrent thread usage.
"""

import asyncio
import json
import logging
from functools import partial
from typing import Any, Optional

from inventree.api import InvenTreeAPI
from inventree.part import Part, PartCategory, BomItem, InternalPrice, PartTestTemplate, PartRelated
from inventree.stock import StockItem, StockLocation, StockItemTracking, StockItemTestResult
from inventree.build import Build, BuildLine
from inventree.purchase_order import PurchaseOrder, PurchaseOrderLineItem, PurchaseOrderExtraLineItem
from inventree.sales_order import SalesOrder, SalesOrderLineItem, SalesOrderExtraLineItem, SalesOrderShipment, SalesOrderAllocation
from inventree.return_order import ReturnOrder, ReturnOrderLineItem, ReturnOrderExtraLineItem
from inventree.company import Company, Contact, Address, SupplierPart, ManufacturerPart, SupplierPriceBreak
from inventree.label import LabelTemplate
from inventree.report import ReportTemplate
from inventree.base import Attachment
from inventree.user import User, Group, Owner

logger = logging.getLogger(__name__)

_sem = asyncio.Semaphore(8)


async def run_sync(fn, *args, **kwargs) -> Any:
    """Run a synchronous function in a thread pool with concurrency limiting."""
    async with _sem:
        return await asyncio.to_thread(partial(fn, *args, **kwargs))


def _serialize(obj) -> dict:
    """Extract serializable data from an inventree object."""
    if hasattr(obj, '_data'):
        return obj._data
    if isinstance(obj, dict):
        return obj
    return {"value": str(obj)}


def _serialize_list(items) -> list[dict]:
    """Serialize a list of inventree objects."""
    if isinstance(items, list):
        return [_serialize(i) for i in items]
    return [_serialize(items)]


class InvenTreeClient:
    """Async wrapper around the inventree-python library."""

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._api: Optional[InvenTreeAPI] = None

    async def connect(self) -> None:
        """Initialize the InvenTree API connection."""
        self._api = await run_sync(
            InvenTreeAPI, self.url, token=self.token, connect=True
        )
        logger.info(f"Connected to InvenTree at {self.url}")

    @property
    def api(self) -> InvenTreeAPI:
        if not self._api:
            raise RuntimeError("InvenTree client not connected. Call connect() first.")
        return self._api

    # ── Generic helpers ──────────────────────────────────────────────

    async def _list(self, cls, **kwargs) -> list[dict]:
        items = await run_sync(cls.list, self.api, **kwargs)
        return _serialize_list(items)

    async def _get(self, cls, pk: int) -> dict:
        obj = await run_sync(cls, self.api, pk)
        return _serialize(obj)

    async def _create(self, cls, data: dict) -> dict | list[dict]:
        result = await run_sync(cls.create, self.api, data)
        # StockItem.create() returns a list since v0.18.0
        if isinstance(result, list):
            return _serialize_list(result)
        return _serialize(result)

    async def _update(self, cls, pk: int, data: dict) -> dict:
        obj = await run_sync(cls, self.api, pk)
        await run_sync(obj.save, data)
        await run_sync(obj.reload)
        return _serialize(obj)

    async def _delete(self, cls, pk: int) -> dict:
        obj = await run_sync(cls, self.api, pk)
        await run_sync(obj.delete)
        return {"deleted": True, "pk": pk}

    async def _count(self, cls, **kwargs) -> int:
        return await run_sync(cls.count, self.api, **kwargs)

    # ── Part operations ──────────────────────────────────────────────

    async def part_list(self, **kwargs) -> list[dict]:
        return await self._list(Part, **kwargs)

    async def part_get(self, pk: int) -> dict:
        return await self._get(Part, pk)

    async def part_create(self, data: dict) -> dict:
        result = await self._create(Part, data)
        return result if isinstance(result, dict) else result[0]

    async def part_update(self, pk: int, data: dict) -> dict:
        return await self._update(Part, pk, data)

    async def part_delete(self, pk: int) -> dict:
        return await self._delete(Part, pk)

    async def part_get_stock(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getStockItems)
        return _serialize_list(items)

    async def part_get_bom(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getBomItems)
        return _serialize_list(items)

    async def part_get_bom_usage(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.isUsedIn)
        return _serialize_list(items)

    async def part_get_suppliers(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getSupplierParts)
        return _serialize_list(items)

    async def part_get_manufacturers(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getManufacturerParts)
        return _serialize_list(items)

    async def part_get_parameters(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getParameters)
        return _serialize_list(items)

    async def part_get_parameter_templates(self) -> list[dict]:
        """List part parameter templates.

        inventree-python has changed class names across versions; fall back to
        hitting the REST endpoint directly if the helper class is unavailable.
        """
        try:
            from inventree.part import PartParameterTemplate
        except Exception:
            result = await run_sync(self.api.get, "/api/parameter/template/")
            if isinstance(result, dict) and "results" in result:
                return result["results"]
            return result if isinstance(result, list) else []
        try:
            return await self._list(PartParameterTemplate)
        except NotImplementedError:
            # inventree-python can gate some classes by API version; direct REST
            # call still works against newer servers.
            result = await run_sync(self.api.get, "/api/parameter/template/")
            if isinstance(result, dict) and "results" in result:
                return result["results"]
            return result if isinstance(result, list) else []


    # ── Part parameter management (generic Parameter API) ────────────

    async def _parameter_api(self, method: str, path: str, data: dict = None):
        """Call the current InvenTree parameter REST API off the event loop."""
        fn = getattr(self.api, method)
        if data is None:
            return await run_sync(fn, path)
        return await run_sync(fn, path, data)

    async def part_create_parameter_template(self, data: dict) -> dict:
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            raise ValueError("Template name is required")
        return await self._parameter_api("post", "/api/parameter/template/", data)

    async def part_update_parameter_template(self, pk: int, data: dict) -> dict:
        if not pk or not data:
            raise ValueError("Template ID and non-empty data required")
        return await self._parameter_api("patch", f"/api/parameter/template/{pk}/", data)

    async def part_create_parameter(self, part_id: int, data: dict) -> dict:
        if not part_id or not isinstance(data, dict):
            raise ValueError("part_id and parameter data are required")
        if not data.get("template") or data.get("data") is None:
            raise ValueError("Parameter requires template ID and data value")
        payload = {"model_type": "part", "model_id": part_id, **data}
        # Never allow data to override the part identity.
        payload["model_type"], payload["model_id"] = "part", part_id
        return await self._parameter_api("post", "/api/parameter/", payload)

    async def part_update_parameter(self, part_id: int, parameter_id: int, data: dict) -> dict:
        if not parameter_id or not data:
            raise ValueError("Parameter ID and non-empty data required")
        current = await self._parameter_api("get", f"/api/parameter/{parameter_id}/")
        if current.get("model_id") != part_id or current.get("model_type") != "part":
            raise ValueError("Parameter does not belong to the specified part")
        if any(k in data for k in ("model_id", "model_type", "template", "pk")):
            raise ValueError("Only parameter value / note can be edited here")
        return await self._parameter_api("patch", f"/api/parameter/{parameter_id}/", data)

    async def part_delete_parameter(self, part_id: int, parameter_id: int) -> dict:
        current = await self._parameter_api("get", f"/api/parameter/{parameter_id}/")
        if current.get("model_id") != part_id or current.get("model_type") != "part":
            raise ValueError("Parameter does not belong to the specified part")
        await self._parameter_api("delete", f"/api/parameter/{parameter_id}/")
        return {"deleted": True, "pk": parameter_id, "part": part_id}

    async def part_upsert_parameters(self, part_id: int, parameters: list[dict]) -> dict:
        """Upsert by template ID; preserve unrelated parameters and report partial failures."""
        if not part_id or not isinstance(parameters, list) or not parameters:
            raise ValueError("part_id and a non-empty parameters list required")
        existing = await self.part_get_parameters(part_id)
        by_template = {}
        for entry in existing:
            key = entry.get("template")
            if key is not None:
                by_template.setdefault(int(key), []).append(entry)
        seen, results = set(), []
        for item in parameters:
            try:
                if not isinstance(item, dict) or not item.get("template") or item.get("data") is None:
                    raise ValueError("Each item requires template ID and data")
                tid = int(item["template"])
                if tid in seen:
                    raise ValueError(f"Duplicate template ID {tid} in request")
                seen.add(tid)
                matches = by_template.get(tid, [])
                if len(matches) > 1:
                    raise ValueError(f"Multiple existing values for template {tid}; resolve manually")
                payload = {k: v for k, v in item.items() if k in ("data", "note")}
                if matches:
                    current = matches[0]
                    if all(current.get(k) == v for k, v in payload.items()):
                        results.append({"template": tid, "action": "unchanged", "pk": current["pk"]})
                    else:
                        updated = await self.part_update_parameter(part_id, current["pk"], payload)
                        results.append({"template": tid, "action": "updated", "result": updated})
                else:
                    created = await self.part_create_parameter(part_id, {**payload, "template": tid})
                    results.append({"template": tid, "action": "created", "result": created})
            except Exception as exc:
                results.append({"template": item.get("template") if isinstance(item, dict) else None,
                                "action": "error", "error": str(exc)})
        return {"part": part_id, "results": results, "success": all(r["action"] != "error" for r in results)}

    async def part_get_test_templates(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getTestTemplates)
        return _serialize_list(items)

    async def part_get_related(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getRelated)
        return _serialize_list(items)

    async def part_get_builds(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getBuilds)
        return _serialize_list(items)

    async def part_get_internal_prices(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        items = await run_sync(part.getInternalPriceList)
        return _serialize_list(items)

    async def part_get_sale_prices(self, pk: int) -> list[dict]:
        part = await run_sync(Part, self.api, pk)
        try:
            items = await run_sync(part.getSalePrice)
        except IndexError:
            # Some InvenTree versions raise when a part has no sale prices.
            return []
        return _serialize_list(items)

    async def part_list_categories(self, **kwargs) -> list[dict]:
        return await self._list(PartCategory, **kwargs)

    async def part_get_category(self, pk: int) -> dict:
        return await self._get(PartCategory, pk)

    async def part_create_category(self, data: dict) -> dict:
        result = await self._create(PartCategory, data)
        return result if isinstance(result, dict) else result[0]

    async def part_delete_category(self, pk: int) -> dict:
        return await self._delete(PartCategory, pk)

    async def part_get_category_parameters(self, pk: int) -> list[dict]:
        cat = await run_sync(PartCategory, self.api, pk)
        items = await run_sync(cat.getCategoryParameterTemplates, fetch_parent=True)
        return _serialize_list(items)

    # ── Stock operations ─────────────────────────────────────────────

    async def stock_list(self, **kwargs) -> list[dict]:
        return await self._list(StockItem, **kwargs)

    async def stock_get(self, pk: int) -> dict:
        return await self._get(StockItem, pk)

    async def stock_create(self, data: dict) -> list[dict]:
        result = await self._create(StockItem, data)
        return result if isinstance(result, list) else [result]

    async def stock_update(self, pk: int, data: dict) -> dict:
        return await self._update(StockItem, pk, data)

    async def stock_delete(self, pk: int) -> dict:
        return await self._delete(StockItem, pk)

    async def stock_transfer(self, pk: int, location: int, quantity: float = None, **kwargs) -> dict:
        item = await run_sync(StockItem, self.api, pk)
        kw = {"location": location, **kwargs}
        if quantity is not None:
            kw["quantity"] = quantity
        await run_sync(item.transferStock, **kw)
        await run_sync(item.reload)
        return _serialize(item)

    async def stock_count(self, pk: int, quantity: float, **kwargs) -> dict:
        item = await run_sync(StockItem, self.api, pk)
        await run_sync(item.countStock, quantity, **kwargs)
        await run_sync(item.reload)
        return _serialize(item)

    async def stock_add(self, pk: int, quantity: float, **kwargs) -> dict:
        item = await run_sync(StockItem, self.api, pk)
        await run_sync(item.addStock, quantity, **kwargs)
        await run_sync(item.reload)
        return _serialize(item)

    async def stock_remove(self, pk: int, quantity: float, **kwargs) -> dict:
        item = await run_sync(StockItem, self.api, pk)
        await run_sync(item.removeStock, quantity, **kwargs)
        await run_sync(item.reload)
        return _serialize(item)

    async def stock_get_by_location(self, location_id: int) -> list[dict]:
        return await self._list(StockItem, location=location_id)

    async def stock_get_by_part(self, part_id: int) -> list[dict]:
        return await self._list(StockItem, part=part_id)

    async def stock_list_locations(self, **kwargs) -> list[dict]:
        return await self._list(StockLocation, **kwargs)

    async def stock_get_location(self, pk: int) -> dict:
        return await self._get(StockLocation, pk)

    async def stock_create_location(self, data: dict) -> dict:
        result = await self._create(StockLocation, data)
        return result if isinstance(result, dict) else result[0]

    async def stock_delete_location(self, pk: int) -> dict:
        return await self._delete(StockLocation, pk)

    async def stock_get_tracking(self, pk: int) -> list[dict]:
        item = await run_sync(StockItem, self.api, pk)
        entries = await run_sync(item.getTrackingEntries)
        return _serialize_list(entries)

    async def stock_upload_test_result(self, pk: int, test_name: str, result: bool, **kwargs) -> dict:
        item = await run_sync(StockItem, self.api, pk)
        res = await run_sync(item.uploadTestResult, test_name, result, **kwargs)
        return _serialize(res)

    async def stock_get_test_results(self, pk: int) -> list[dict]:
        item = await run_sync(StockItem, self.api, pk)
        results = await run_sync(item.getTestResults)
        return _serialize_list(results)

    # ── Build order operations ───────────────────────────────────────

    async def build_list(self, **kwargs) -> list[dict]:
        return await self._list(Build, **kwargs)

    async def build_get(self, pk: int) -> dict:
        return await self._get(Build, pk)

    async def build_create(self, data: dict) -> dict:
        result = await self._create(Build, data)
        return result if isinstance(result, dict) else result[0]

    async def build_update(self, pk: int, data: dict) -> dict:
        return await self._update(Build, pk, data)

    async def build_complete(self, pk: int, **kwargs) -> dict:
        build = await run_sync(Build, self.api, pk)
        await run_sync(build.complete, **kwargs)
        await run_sync(build.reload)
        return _serialize(build)

    async def build_cancel(self, pk: int) -> dict:
        build = await run_sync(Build, self.api, pk)
        await run_sync(build.cancel)
        await run_sync(build.reload)
        return _serialize(build)

    async def build_delete(self, pk: int) -> dict:
        return await self._delete(Build, pk)

    async def build_get_lines(self, pk: int) -> list[dict]:
        build = await run_sync(Build, self.api, pk)
        lines = await run_sync(build.getLines)
        return _serialize_list(lines)

    async def build_allocate(self, pk: int, items: list[dict]) -> dict:
        """Allocate stock items to a build order via API."""
        data = {"items": items}
        result = await run_sync(self.api.post, f"/api/build/{pk}/allocate/", data)
        return result if isinstance(result, dict) else {"allocated": True}

    async def build_get_outputs(self, pk: int) -> list[dict]:
        """Get build outputs (completed stock items)."""
        items = await self._list(StockItem, build=pk, is_building=True)
        return items

    # ── Purchase order operations ────────────────────────────────────

    async def po_list(self, **kwargs) -> list[dict]:
        return await self._list(PurchaseOrder, **kwargs)

    async def po_get(self, pk: int) -> dict:
        return await self._get(PurchaseOrder, pk)

    async def po_create(self, data: dict) -> dict:
        result = await self._create(PurchaseOrder, data)
        return result if isinstance(result, dict) else result[0]

    async def po_update(self, pk: int, data: dict) -> dict:
        return await self._update(PurchaseOrder, pk, data)

    async def po_issue(self, pk: int) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        await run_sync(po.issue)
        await run_sync(po.reload)
        return _serialize(po)

    async def po_receive(self, pk: int, location: int, items: list[dict] = None) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        if items:
            # Receive specific line items
            for item in items:
                line = await run_sync(PurchaseOrderLineItem, self.api, item["line_item"])
                await run_sync(
                    line.receive,
                    quantity=item.get("quantity"),
                    location=location,
                    status=item.get("status", 10),
                )
        else:
            await run_sync(po.receiveAll, location=location)
        await run_sync(po.reload)
        return _serialize(po)

    async def po_complete(self, pk: int) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        await run_sync(po.complete)
        await run_sync(po.reload)
        return _serialize(po)

    async def po_cancel(self, pk: int) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        await run_sync(po.cancel)
        await run_sync(po.reload)
        return _serialize(po)

    async def po_delete(self, pk: int) -> dict:
        return await self._delete(PurchaseOrder, pk)

    async def po_hold(self, pk: int) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        await run_sync(po.hold)
        await run_sync(po.reload)
        return _serialize(po)

    async def po_add_line_item(self, pk: int, data: dict) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        item = await run_sync(po.addLineItem, **data)
        return _serialize(item)

    async def po_add_extra_line_item(self, pk: int, data: dict) -> dict:
        po = await run_sync(PurchaseOrder, self.api, pk)
        item = await run_sync(po.addExtraLineItem, **data)
        return _serialize(item)

    async def po_get_line_items(self, pk: int) -> list[dict]:
        po = await run_sync(PurchaseOrder, self.api, pk)
        items = await run_sync(po.getLineItems)
        return _serialize_list(items)

    # ── Sales order operations ───────────────────────────────────────

    async def so_list(self, **kwargs) -> list[dict]:
        return await self._list(SalesOrder, **kwargs)

    async def so_get(self, pk: int) -> dict:
        return await self._get(SalesOrder, pk)

    async def so_create(self, data: dict) -> dict:
        result = await self._create(SalesOrder, data)
        return result if isinstance(result, dict) else result[0]

    async def so_update(self, pk: int, data: dict) -> dict:
        return await self._update(SalesOrder, pk, data)

    async def so_complete(self, pk: int) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        await run_sync(so.complete)
        await run_sync(so.reload)
        return _serialize(so)

    async def so_cancel(self, pk: int) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        await run_sync(so.cancel)
        await run_sync(so.reload)
        return _serialize(so)

    async def so_hold(self, pk: int) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        await run_sync(so.hold)
        await run_sync(so.reload)
        return _serialize(so)

    async def so_add_line_item(self, pk: int, data: dict) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        item = await run_sync(so.addLineItem, **data)
        return _serialize(item)

    async def so_add_extra_line_item(self, pk: int, data: dict) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        item = await run_sync(so.addExtraLineItem, **data)
        return _serialize(item)

    async def so_get_line_items(self, pk: int) -> list[dict]:
        so = await run_sync(SalesOrder, self.api, pk)
        items = await run_sync(so.getLineItems)
        return _serialize_list(items)

    async def so_create_shipment(self, pk: int, reference: str, **kwargs) -> dict:
        so = await run_sync(SalesOrder, self.api, pk)
        shipment = await run_sync(so.addShipment, reference, **kwargs)
        return _serialize(shipment)

    async def so_complete_shipment(self, shipment_id: int, **kwargs) -> dict:
        shipment = await run_sync(SalesOrderShipment, self.api, shipment_id)
        await run_sync(shipment.complete, **kwargs)
        await run_sync(shipment.reload)
        return _serialize(shipment)

    async def so_get_allocations(self, pk: int) -> list[dict]:
        return await self._list(SalesOrderAllocation, order=pk)

    # ── Return order operations ──────────────────────────────────────

    async def ro_list(self, **kwargs) -> list[dict]:
        return await self._list(ReturnOrder, **kwargs)

    async def ro_get(self, pk: int) -> dict:
        return await self._get(ReturnOrder, pk)

    async def ro_create(self, data: dict) -> dict:
        result = await self._create(ReturnOrder, data)
        return result if isinstance(result, dict) else result[0]

    async def ro_update(self, pk: int, data: dict) -> dict:
        return await self._update(ReturnOrder, pk, data)

    async def ro_complete(self, pk: int) -> dict:
        ro = await run_sync(ReturnOrder, self.api, pk)
        await run_sync(ro.complete)
        await run_sync(ro.reload)
        return _serialize(ro)

    async def ro_cancel(self, pk: int) -> dict:
        ro = await run_sync(ReturnOrder, self.api, pk)
        await run_sync(ro.cancel)
        await run_sync(ro.reload)
        return _serialize(ro)

    async def ro_add_line_item(self, pk: int, data: dict) -> dict:
        ro = await run_sync(ReturnOrder, self.api, pk)
        item = await run_sync(ro.addLineItem, **data)
        return _serialize(item)

    async def ro_get_line_items(self, pk: int) -> list[dict]:
        ro = await run_sync(ReturnOrder, self.api, pk)
        items = await run_sync(ro.getLineItems)
        return _serialize_list(items)

    # ── Company operations ───────────────────────────────────────────

    async def company_list(self, **kwargs) -> list[dict]:
        return await self._list(Company, **kwargs)

    async def company_get(self, pk: int) -> dict:
        return await self._get(Company, pk)

    async def company_create(self, data: dict) -> dict:
        result = await self._create(Company, data)
        return result if isinstance(result, dict) else result[0]

    async def company_update(self, pk: int, data: dict) -> dict:
        return await self._update(Company, pk, data)

    async def company_delete(self, pk: int) -> dict:
        return await self._delete(Company, pk)

    async def company_list_supplier_parts(self, **kwargs) -> list[dict]:
        return await self._list(SupplierPart, **kwargs)

    async def company_get_supplier_part(self, pk: int) -> dict:
        return await self._get(SupplierPart, pk)

    async def company_list_manufacturer_parts(self, **kwargs) -> list[dict]:
        return await self._list(ManufacturerPart, **kwargs)

    async def company_get_manufacturer_part(self, pk: int) -> dict:
        return await self._get(ManufacturerPart, pk)

    async def company_list_contacts(self, company_id: int) -> list[dict]:
        return await self._list(Contact, company=company_id)

    async def company_list_addresses(self, company_id: int) -> list[dict]:
        return await self._list(Address, company=company_id)

    async def company_get_price_breaks(self, supplier_part_id: int) -> list[dict]:
        sp = await run_sync(SupplierPart, self.api, supplier_part_id)
        breaks = await run_sync(sp.getPriceBreaks)
        return _serialize_list(breaks)

    # ── Barcode operations ───────────────────────────────────────────

    async def barcode_scan(self, barcode: str) -> dict:
        result = await run_sync(self.api.scanBarcode, barcode)
        return result

    async def barcode_assign(self, model_type: str, pk: int, barcode: str) -> dict:
        """Assign barcode to a model instance."""
        MODEL_MAP = {
            "part": Part, "stockitem": StockItem, "stocklocation": StockLocation,
            "company": Company,
        }
        cls = MODEL_MAP.get(model_type.lower())
        if not cls:
            raise ValueError(f"Unknown model type: {model_type}. Use: {', '.join(MODEL_MAP.keys())}")
        obj = await run_sync(cls, self.api, pk)
        await run_sync(obj.assignBarcode, barcode)
        return {"assigned": True, "model": model_type, "pk": pk, "barcode": barcode}

    async def barcode_unassign(self, model_type: str, pk: int) -> dict:
        MODEL_MAP = {
            "part": Part, "stockitem": StockItem, "stocklocation": StockLocation,
            "company": Company,
        }
        cls = MODEL_MAP.get(model_type.lower())
        if not cls:
            raise ValueError(f"Unknown model type: {model_type}")
        obj = await run_sync(cls, self.api, pk)
        await run_sync(obj.unassignBarcode)
        return {"unassigned": True, "model": model_type, "pk": pk}

    async def barcode_lookup(self, barcode: str) -> dict:
        return await self.barcode_scan(barcode)

    # ── Label operations ─────────────────────────────────────────────

    async def label_list_templates(self, label_type: str = None) -> list[dict]:
        filters = {}
        if label_type:
            filters["model_type"] = label_type
        return await self._list(LabelTemplate, **filters)

    async def label_download_template(self, template_id: int, destination: str) -> dict:
        """Download a label template file to destination path."""
        label = await run_sync(LabelTemplate, self.api, template_id)
        result = await run_sync(label.downloadTemplate, destination, overwrite=True)
        return {"downloaded": bool(result), "template_id": template_id, "destination": destination}

    async def label_print(self, label_type: str, template_id: int, item_ids: list[int]) -> dict:
        """Print labels for items."""
        label = await run_sync(LabelTemplate, self.api, template_id)
        ITEM_KEY = {"part": "parts", "stock": "items", "location": "locations"}
        url = f"{LabelTemplate.URL}{template_id}/print/"
        key = ITEM_KEY.get(label_type, "items")
        result = await run_sync(self.api.post, url, {key: item_ids})
        return result if isinstance(result, dict) else {"printed": True}

    # ── Report operations ────────────────────────────────────────────

    async def report_list_templates(self) -> list[dict]:
        """List available report templates via API."""
        return await self._list(ReportTemplate)

    async def report_download_template(self, template_id: int, destination: str) -> dict:
        """Download a report template file to destination path."""
        report = await run_sync(ReportTemplate, self.api, template_id)
        result = await run_sync(report.downloadTemplate, destination, overwrite=True)
        return {"downloaded": bool(result), "template_id": template_id, "destination": destination}

    async def report_print(self, template_id: int, item_ids: list[int], model_type: str) -> dict:
        """Print a report for items."""
        url = f"/api/report/{template_id}/print/"
        result = await run_sync(self.api.post, url, {"items": item_ids, "model_type": model_type})
        return result if isinstance(result, dict) else {"printed": True}

    # ── Attachment operations ────────────────────────────────────────

    async def attachment_list(self, model_type: str, model_id: int) -> list[dict]:
        """List attachments for a model."""
        url = f"/api/attachment/?model_type={model_type}&model_id={model_id}"
        result = await run_sync(self.api.get, url)
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        return result if isinstance(result, list) else []

    async def attachment_upload(self, model_type: str, model_id: int, file_path: str, comment: str = "") -> dict:
        """Upload a file attachment."""
        # Map to the model class for attachment mixin
        MODEL_MAP = {
            "part": Part, "stockitem": StockItem, "stocklocation": StockLocation,
            "build": Build, "purchaseorder": PurchaseOrder,
            "salesorder": SalesOrder, "company": Company,
        }
        cls = MODEL_MAP.get(model_type.lower())
        if not cls:
            raise ValueError(f"Unknown model type: {model_type}")
        obj = await run_sync(cls, self.api, model_id)
        result = await run_sync(obj.uploadAttachment, file_path, comment=comment)
        return _serialize(result) if result else {"uploaded": True}

    async def attachment_upload_link(self, model_type: str, model_id: int, link: str, comment: str = "") -> dict:
        MODEL_MAP = {
            "part": Part, "stockitem": StockItem, "stocklocation": StockLocation,
            "build": Build, "purchaseorder": PurchaseOrder,
            "salesorder": SalesOrder, "company": Company,
        }
        cls = MODEL_MAP.get(model_type.lower())
        if not cls:
            raise ValueError(f"Unknown model type: {model_type}")
        obj = await run_sync(cls, self.api, model_id)
        result = await run_sync(obj.addLinkAttachment, link, comment=comment)
        return _serialize(result) if result else {"uploaded": True}

    async def attachment_download(self, attachment_id: int, destination: str) -> dict:
        """Download an attachment file to destination path."""
        att = await run_sync(Attachment, self.api, attachment_id)
        result = await run_sync(att.download, destination, overwrite=True)
        return {"downloaded": bool(result), "attachment_id": attachment_id, "destination": destination}

    async def attachment_delete(self, attachment_id: int) -> dict:
        """Delete an attachment by ID."""
        await run_sync(self.api.delete, f"/api/attachment/{attachment_id}/")
        return {"deleted": True, "pk": attachment_id}

    # ── System operations ────────────────────────────────────────────

    async def system_health(self) -> dict:
        """Check server health."""
        result = await run_sync(self.api.get, "/api/")
        return result if isinstance(result, dict) else {"status": "ok"}

    async def system_version(self) -> dict:
        """Get server version info."""
        return {
            "server": self.url,
            "api_version": getattr(self._api, 'api_version', None),
        }

    async def system_settings(self) -> list[dict]:
        """List global settings."""
        result = await run_sync(self.api.get, "/api/settings/global/")
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        return result if isinstance(result, list) else []

    async def system_list_users(self) -> list[dict]:
        return await self._list(User)

    async def system_get_user(self, pk: int) -> dict:
        return await self._get(User, pk)

    async def system_list_groups(self) -> list[dict]:
        return await self._list(Group)

    async def system_list_owners(self) -> list[dict]:
        return await self._list(Owner)

    async def system_get_project_codes(self) -> list[dict]:
        """List project codes."""
        result = await run_sync(self.api.get, "/api/project-code/")
        if isinstance(result, dict) and "results" in result:
            return result["results"]
        return result if isinstance(result, list) else []

    async def system_list_currencies(self) -> list[dict]:
        """List available currencies."""
        result = await run_sync(self.api.get, "/api/currency/exchange/")
        if isinstance(result, dict):
            return [{"code": k, "rate": v} for k, v in result.items()]
        return []
