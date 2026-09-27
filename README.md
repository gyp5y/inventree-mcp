# InvenTree MCP Server

MCP server for [InvenTree](https://inventree.org) inventory management. Provides 12 parameterized tools covering 118 operations for parts, stock, build orders, purchase/sales/return orders, companies, barcodes, labels, reports, attachments, and system administration.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- An InvenTree instance with API access enabled
- An API token (generate from InvenTree > Settings > API Tokens)

## Setup

```bash
# Clone the repository
git clone https://github.com/puran-water/inventree-mcp.git
cd inventree-mcp

# Copy the example environment file and fill in your values
cp .env.example .env

# Install dependencies
uv sync
```

Edit `.env` with your InvenTree instance URL and API token:

```
INVENTREE_URL=https://your-inventree-instance.example.com
INVENTREE_TOKEN=your-api-token-here
```

## Usage

### STDIO mode (default)

```bash
uv run python server.py
```

### SSE mode (HTTP transport)

```bash
uv run python server.py sse --port 3074
```

### Claude Desktop / MCP client configuration

Add to your MCP client config (e.g. `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "inventree-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/inventree-mcp", "python", "server.py"],
      "env": {
        "INVENTREE_URL": "https://your-inventree-instance.example.com",
        "INVENTREE_TOKEN": "your-api-token-here"
      }
    }
  }
}
```

Or for SSE transport:

```json
{
  "mcpServers": {
    "inventree-mcp": {
      "url": "http://localhost:3074/sse"
    }
  }
}
```

## Tools

Each tool uses a parameterized `operation` field to select the specific action.

| Tool | Operations | Description |
|------|-----------|-------------|
| `part` | 21 | Part & category management (list, get, create, update, delete, BOM, suppliers, parameters) |
| `stock` | 17 | Stock item & location management (list, get, create, update, transfer, count, add, remove, location operations) |
| `build_order` | 9 | Manufacturing build orders (list, get, create, update, allocate, complete, cancel) |
| `purchase_order` | 12 | Purchase order lifecycle (list, get, create, update, issue, receive, complete) |
| `sales_order` | 14 | Sales order lifecycle (list, get, create, shipments, allocations) |
| `return_order` | 8 | Return order management |
| `company` | 12 | Suppliers, manufacturers, customers, contacts, addresses |
| `barcode` | 4 | Barcode scan, assign, unassign, lookup |
| `label` | 5 | Label template listing and printing |
| `report` | 3 | Report template listing and generation |
| `attachment` | 5 | File attachments on any object (upload, download, delete) |
| `system` | 8 | Health, version, settings, users, groups, currencies |

## Architecture

- **`server.py`** — FastMCP server with 12 parameterized tools and dual transport (STDIO/SSE)
- **`client.py`** — Async adapter wrapping the official [inventree-python](https://github.com/inventree/inventree-python) library via `asyncio.to_thread()` with a semaphore for concurrency control

The `inventree-python` library is synchronous (requests-based). All calls are offloaded to threads to maintain async compatibility with the MCP framework.

## Stock item and location updates

The `stock` tool keeps stock item updates and location updates explicit:

```json
{"operation":"update","pk":123,"data":{"status":10}}
{"operation":"update_location","pk":45,"data":{"name":"SMD reels","description":"Drawer A3"}}
```

For `operation="update"`, `pk` is always a `StockItem` ID. For
`operation="update_location"`, `pk` is always a `StockLocation` ID.

## License

MIT

## Part parameter management

The `part` tool supports six additional operations:
`create_parameter_template`, `update_parameter_template`,
`create_parameter`, `update_parameter`, `delete_parameter`,
and `upsert_parameters`. Use `get_parameter_templates` to discover
template IDs and `get_parameters` to inspect existing part parameters.

Example calls (the MCP `part` tool arguments):

```json
{"operation":"create_parameter_template","data":{"name":"Drain-source voltage","units":"V"}}
{"operation":"create_parameter","pk":123,"data":{"template":12,"data":"60 V"}}
{"operation":"upsert_parameters","pk":123,"data":{"parameters":[{"template":12,"data":"60 V"},{"template":13,"data":"5 A"}]}}
{"operation":"update_parameter","pk":123,"data":{"parameter_id":456,"data":"55 V"}}
{"operation":"delete_parameter","pk":123,"data":{"parameter_id":456}}
```

`pk` denotes the part ID except for `update_parameter_template`, where
it is the template ID. Upsert is keyed by template ID and refuses to guess
when an existing part has multiple values for one template. Batch results
report individual errors; they are not an atomic transaction. Verify exact
values and units against the manufacturer datasheet before insertion.

### Parameter ownership and shared-template safety

Updating or deleting a parameter checks its ID against the requested part's
`get_parameters` collection, rather than relying on the version-dependent
`model_type` / `model_id` representation of the generic parameter endpoint.
Only `data` (value) and `note` may be changed by `update_parameter`.

A parameter template is shared by all parts using it. To prevent accidentally
changing every capacitor when correcting one capacitance, `update_parameter_template`
requires an explicit `data.allow_shared_template_change: true` flag. Use
`update_parameter` / `upsert_parameters` to correct one component's value.

## Upload images from chat

ChatGPT can pass an attached original through the native `image_file` parameter.
The MCP server downloads its short-lived HTTPS URL, validates the actual image
signature and size, and forwards the original bytes to InvenTree.

For a part's **main image**, call the `part` tool:

```json
{"operation":"upload_image","pk":123,"image_file":"<attached original>","data":{"replace":false}}
```

Use `data.replace=true` to explicitly replace an existing main image. For
additional photographs (marking, package, tray), call `attachment`:

```json
{"operation":"upload_image","model_type":"part","model_id":123,"image_file":"<attached original>","comment":"Original photo from inventory chat"}
```

`attachment.upload_image` also supports `model_type="stockitem"`. The Base64
input remains available for MCP clients without native file parameters. The
existing `attachment.upload` operation accepts a file path **on the MCP server**; it
cannot read a local path from a different ChatGPT or Docker host. The `part`
`upload_image` operation also supports `data.file_path` on the MCP server.

Supported formats: JPEG, PNG, GIF, WebP (max 10 MiB). Plain base64 and
`data:image/...;base64,...` data URLs are accepted; whitespace and omitted
padding are normalized before the decoded bytes are checked for a real image
signature. Chat bytes are written into a temporary directory for the duration of
the upload and removed after it. The image persists in InvenTree after
successful upload. InvenTree must have media storage correctly configured and
the API token must allow file upload.
