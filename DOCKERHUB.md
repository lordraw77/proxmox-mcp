# lordraw/proxmox-mcp

**MCP server for Proxmox VE** — exposes 69 management tools over the [Model Context Protocol](https://modelcontextprotocol.io/) (stdio transport).

Connect any MCP-compatible AI client (Claude Desktop, custom agents, …) to your Proxmox cluster and manage it in natural language.

---

## What is this?

`proxmox-mcp` is a lightweight MCP server that wraps the Proxmox VE REST API.  
An AI agent or MCP client spawns this container, sends JSON-RPC tool calls over stdin/stdout, and gets structured results back — no HTTP port, no daemon, no persistent process.

```
AI client  ──stdin/stdout──►  lordraw/proxmox-mcp  ──HTTPS:8006──►  Proxmox VE
```

---

## Quick start

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--env-file", "/path/to/.env",
        "lordraw/proxmox-mcp"
      ]
    }
  }
}
```

### Any MCP client (Python example)

```python
from mcp.client.stdio import stdio_client, StdioServerParameters

params = StdioServerParameters(
    command="docker",
    args=["run", "--rm", "-i", "--env-file", ".env", "lordraw/proxmox-mcp"],
    env={},
)

async with stdio_client(params) as (read, write):
    ...
```

---

## Configuration

Pass credentials via `--env-file` or individual `-e` flags. **Never bake secrets into the image.**

### Token auth (recommended)

```dotenv
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_TOKEN_ID=mytoken
PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
PROXMOX_VERIFY_SSL=false
```

### Password auth

```dotenv
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=your_password
PROXMOX_VERIFY_SSL=false
```

| Variable | Required | Description |
|---|---|---|
| `PROXMOX_HOST` | ✅ | Proxmox VE host IP or hostname |
| `PROXMOX_PORT` | — | API port (default: `8006`) |
| `PROXMOX_USER` | ✅ | User with realm, e.g. `root@pam` |
| `PROXMOX_TOKEN_ID` | ✅* | API token name (*token auth*) |
| `PROXMOX_TOKEN_SECRET` | ✅* | API token secret UUID (*token auth*) |
| `PROXMOX_PASSWORD` | ✅* | Account password (*password auth*) |
| `PROXMOX_VERIFY_SSL` | — | Verify TLS cert (default: `false`) |

\* Token auth takes priority when both are present.

---

## Available tools (69 total)

| Category | Count | Tools |
|---|---|---|
| **Informational** | 11 | `list_nodes`, `list_vms`, `vm_status`, `vm_config`, `cluster_resources`, `cluster_tasks`, `node_tasks`, `list_snapshots`, `storage_content`, `node_network`, `list_storage` |
| **Lifecycle** | 4 | `vm_start`, `vm_stop`, `vm_shutdown`, `vm_reboot` |
| **Snapshots** | 3 | `create_snapshot`, `delete_snapshot`, `rollback_snapshot` |
| **Backup** | 3 | `list_backups`, `create_backup`, `restore_backup` |
| **Clone & provisioning** | 4 | `vm_clone`, `vm_create`, `vm_delete`, `vm_resize_disk` |
| **Disk management** | 4 | `vm_move_disk`, `vm_unlink_disk`, `list_node_disks`, `vm_template` |
| **Firewall** | 5 | `list_firewall_rules`, `create_firewall_rule`, `delete_firewall_rule`, `list_firewall_aliases`, `list_firewall_ipsets` |
| **Metrics — RRD** | 2 | `node_rrddata`, `vm_rrddata` |
| **High Availability** | 3 | `cluster_status`, `ha_resources`, `ha_groups` |
| **Ceph** | 4 | `ceph_status`, `ceph_health`, `ceph_osds`, `ceph_pools` |
| **QEMU Guest Agent** | 3 | `vm_agent_exec`, `vm_agent_info`, `vm_agent_network` |
| **Backup jobs** | 2 | `list_backup_jobs`, `prune_backups` |
| **Replication** | 3 | `list_replication`, `create_replication`, `delete_replication` |
| **Node — OS & system** | 8 | `node_apt_updates`, `node_syslog`, `node_dns`, `node_subscription`, `node_reboot`, `node_shutdown`, `node_apt_upgrade`, `node_certificates` |
| **Users & ACL** | 4 | `list_users`, `list_tokens`, `list_acl`, `list_pools` |
| **SDN** | 2 | `list_vnets`, `list_sdn_zones` |
| **Notifications** | 2 | `list_notification_endpoints`, `list_notification_matchers` |
| **Console** | 1 | `vm_console_url` |
| **Migration** | 1 | `vm_migrate` |

---

## Image tags

| Tag | Description |
|---|---|
| `latest` | Latest stable build from `main` |
| `x.y.z` | Pinned release version |

---

## Source

GitHub: [lordraw77/proxmox-mcp](https://github.com/lordraw77/proxmox-mcp)
