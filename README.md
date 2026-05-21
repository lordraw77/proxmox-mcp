# Proxmox MCP

An AI-powered management interface for **Proxmox VE** built on the
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

An LLM (via [OpenRouter](https://openrouter.ai)) can query and control a
Proxmox cluster in natural language by calling 46 structured tools exposed
through the MCP server — from reading cluster metrics to creating backups,
cloning VMs, managing firewall rules and more.

---

## Architecture

```
User (terminal)
      │  natural language
      ▼
agent.py  ──────────────────────────────────────────────────────────────►  OpenRouter API
  OpenAI-compatible client                                                   (any LLM model)
      │  tool_calls (JSON)
      ▼
server.py  (MCP server, stdio transport)
  proxmoxer REST client
      │  HTTPS / port 8006
      ▼
Proxmox VE cluster
```

| Component | File | Role |
|-----------|------|------|
| MCP Server | `server.py` | Wraps the Proxmox REST API as 46 MCP tools |
| AI Agent | `agent.py` | Drives the LLM ↔ MCP tool-use loop; interactive CLI |
| Utilities | `util.py` | Formats raw bytes / seconds / fractions for display |

---

## Requirements

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Python | ≥ 3.12 | Required by the `mcp` SDK |
| `mcp` | 1.27+ | MCP server/client framework |
| `proxmoxer` | 2.x | Proxmox VE REST API wrapper |
| `python-dotenv` | 1.x | `.env` file loader |
| `openai` | 2.x | OpenAI-compatible client (used for OpenRouter) |
| `requests` | any | HTTP backend for proxmoxer |

---

## Installation

```bash
# 1. Clone or copy the project
cd /opt/proxmox-mcp

# 2. Create a virtualenv with Python 3.12
python3.12 -m venv .venv

# 3. Install dependencies
.venv/bin/pip install mcp proxmoxer python-dotenv requests "openai>=2"
```

---

## Configuration

Create a `.env` file in the project root.  Two authentication modes are
supported — token auth is preferred for automation because it does not
expose the account password and can be scoped to specific privileges.

### Token authentication (recommended)

```dotenv
# Proxmox VE — token auth
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam             # user that owns the token (realm required)
PROXMOX_TOKEN_ID=mytoken          # token name shown in the Proxmox UI
PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx   # UUID shown at creation
PROXMOX_VERIFY_SSL=false          # true if your node has a valid TLS certificate

# OpenRouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-opus-4.5
```

**How to create a token in Proxmox:**
1. Go to **Datacenter → Permissions → API Tokens → Add**.
2. Select the user, give the token a name, and uncheck *Privilege Separation*
   if you want the token to inherit all user permissions.
3. Copy the displayed UUID secret — it is shown only once.

### Password authentication (fallback)

```dotenv
# Proxmox VE — password auth
PROXMOX_HOST=192.168.1.10
PROXMOX_PORT=8006
PROXMOX_USER=root@pam
PROXMOX_PASSWORD=your_password
PROXMOX_VERIFY_SSL=false

# OpenRouter
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-opus-4.5
```

> **Auth priority** — if `PROXMOX_TOKEN_ID` and `PROXMOX_TOKEN_SECRET` are
> both set, token auth is used and `PROXMOX_PASSWORD` is ignored.

> **TLS** — set `PROXMOX_VERIFY_SSL=true` only if your Proxmox node has a
> certificate signed by a trusted CA.  Most homelab setups use self-signed
> certs and should keep it `false`.

---

## Usage

### Interactive agent (recommended)

```bash
cd /opt/proxmox-mcp
.venv/bin/python agent.py
```

```
Proxmox Agent — model: anthropic/claude-opus-4.5
Type a question or 'exit' to quit.

>>> How many nodes are in the cluster?
>>> List all running VMs on node pve
>>> Show me the CPU history of node pve for the last day
>>> Clone VM 100 on node pve to ID 200
>>> Take a snapshot of VM 100 on node pve named before-update
>>> exit
```

### MCP server standalone (for Claude Desktop or other MCP clients)

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "/opt/proxmox-mcp/.venv/bin/python",
      "args": ["/opt/proxmox-mcp/server.py"]
    }
  }
}
```

---

## Available Tools (46 total)

### Informational — read-only (11)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `list_nodes` | All cluster nodes — status, CPU, memory, disk, uptime | — |
| `list_vms` | QEMU VMs and LXC containers on a node with live metrics | `node` |
| `list_storage` | Cluster storage pools — type and supported content | — |
| `vm_status` | Live resource usage for a single VM or container | `node`, `vmid`, `type` |
| `vm_config` | Full stored configuration (CPU, RAM, disks, network, boot) | `node`, `vmid`, `type` |
| `cluster_resources` | Unified view of all cluster resources with optional type filter | — |
| `cluster_tasks` | Recent and running tasks across the whole cluster | — |
| `node_tasks` | Recent and running tasks on a specific node | `node` |
| `list_snapshots` | Snapshots for a VM or container with timestamps | `node`, `vmid`, `type` |
| `storage_content` | Objects in a storage pool (ISO, backup, disk images) | `node`, `storage` |
| `node_network` | Network interfaces and bridges configured on a node | `node` |

### Backup (3)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `list_backups` | List vzdump backup archives in a storage pool | `node`, `storage` |
| `create_backup` | Start a vzdump backup job for a VM or container | `node`, `vmid`, `storage` |
| `restore_backup` | Restore a VM or container from a backup archive | `node`, `vmid`, `type`, `volid`, `storage` |

### Clone & provisioning (4)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `vm_clone` | Clone a VM or container (linked or full clone) | `node`, `vmid`, `type`, `newid` |
| `vm_create` | Create a new VM (QEMU) or container (LXC) from scratch | `node`, `vmid`, `type` |
| `vm_delete` | Permanently delete a VM or container and its disks ⚠ | `node`, `vmid`, `type` |
| `vm_resize_disk` | Extend a disk attached to a VM or container | `node`, `vmid`, `type`, `disk`, `size` |

### Firewall (5)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `list_firewall_rules` | Firewall rules at cluster, node or VM level | `level` |
| `create_firewall_rule` | Add a firewall rule | `level`, `action`, `direction` |
| `delete_firewall_rule` | Delete a firewall rule by position index | `level`, `pos` |
| `list_firewall_aliases` | Cluster-wide named IP aliases | — |
| `list_firewall_ipsets` | Cluster-wide named IP groups | — |

### Historical metrics — RRD (2)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `node_rrddata` | Historical CPU / memory / network / disk metrics for a node | `node` |
| `vm_rrddata` | Historical CPU / memory / network / disk metrics for a VM/CT | `node`, `vmid`, `type` |

### High Availability (3)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `cluster_status` | Cluster quorum and HA manager state | — |
| `ha_resources` | Resources managed by the HA manager | — |
| `ha_groups` | HA groups and node priority assignments | — |

### Node — OS & system (4)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `node_apt_updates` | Available APT package upgrades on a node | `node` |
| `node_syslog` | Recent system log entries from a node | `node` |
| `node_dns` | DNS resolver configuration of a node | `node` |
| `node_subscription` | Proxmox VE subscription status | `node` |

### Users & access control (4)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `list_users` | All Proxmox users with realm and expiry | — |
| `list_tokens` | API tokens for a specific user | `userid` |
| `list_acl` | All ACL entries — who can do what on which path | — |
| `list_pools` | Resource pools with member VMs and storage | — |

### Software Defined Networking (2)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `list_vnets` | SDN virtual networks | — |
| `list_sdn_zones` | SDN zones (VXLAN, EVPN, Simple, QinQ) | — |

### Reversible lifecycle actions (4)

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `vm_start` | Power on a stopped VM or container | `node`, `vmid`, `type` |
| `vm_stop` | Force power off (hard reset) | `node`, `vmid`, `type` |
| `vm_shutdown` | ACPI graceful shutdown | `node`, `vmid`, `type` |
| `vm_reboot` | Reboot a running VM or container | `node`, `vmid`, `type` |

### Persistent state changes ⚠ (4)

> These operations modify cluster state permanently or irreversibly.

| Tool | Description | Required arguments |
|------|-------------|--------------------|
| `create_snapshot` | Create a point-in-time snapshot | `node`, `vmid`, `type`, `name` |
| `delete_snapshot` | Permanently remove a snapshot | `node`, `vmid`, `type`, `name` |
| `rollback_snapshot` | Restore VM/CT to snapshot state — discards all subsequent changes | `node`, `vmid`, `type`, `name` |
| `vm_migrate` | Migrate to another node (offline or live) | `node`, `vmid`, `type`, `target` |

---

## Common argument reference

| Argument | Type | Description |
|----------|------|-------------|
| `node` | string | Proxmox node name (e.g. `"pve"`) |
| `vmid` | integer | VM or container ID (e.g. `100`) |
| `type` | `"qemu"` \| `"lxc"` | Resource type |
| `storage` | string | Storage pool name (e.g. `"local"`, `"local-lvm"`) |
| `name` | string | Snapshot name, VM name, or clone name |
| `newid` | integer | Target ID for clone operations |
| `target` | string | Destination node name for migration |
| `online` | boolean | Live migration without downtime (QEMU only) |
| `timeframe` | `"hour"` \| `"day"` \| `"week"` \| `"month"` \| `"year"` | RRD time window |
| `level` | `"cluster"` \| `"node"` \| `"vm"` | Firewall scope |
| `pos` | integer | Firewall rule position index (zero-based) |
| `disk` | string | Disk identifier, e.g. `"scsi0"`, `"virtio0"`, `"rootfs"` |
| `size` | string | Disk size, absolute `"50G"` or relative `"+10G"` |
| `volid` | string | Volume ID, e.g. `"local:backup/vzdump-qemu-100-....vma.zst"` |
| `userid` | string | Full Proxmox user ID including realm, e.g. `"root@pam"` |

---

## How the agent loop works

```
┌─────────────────────────────────────────────────────────────┐
│  ask(question)                                              │
│                                                             │
│  messages = [{"role": "user", "content": question}]        │
│                                                             │
│  loop:                                                      │
│    response = LLM(messages, tools=all_46_tools)             │
│    messages.append(response.message)                        │
│                                                             │
│    if no tool_calls:                                        │
│        return response.message.content   ◄── final answer  │
│                                                             │
│    for each tool_call:                                      │
│        result = mcp_client.call_tool(name, args)           │
│        messages.append({"role": "tool", ...result})        │
│    ↑ loop                                                   │
└─────────────────────────────────────────────────────────────┘
```

Each `ask()` call opens a fresh MCP session (spawning `server.py` as a
subprocess).  Conversation history is preserved within a single question
but **not** carried across separate questions.

---

## Selecting a model

```dotenv
# Fast and inexpensive
OPENROUTER_MODEL=anthropic/claude-haiku-4-5

# Balanced — recommended for most tasks
OPENROUTER_MODEL=anthropic/claude-opus-4.5

# Google alternative
OPENROUTER_MODEL=google/gemini-2.5-pro

# Free tier — OpenRouter picks a free model
OPENROUTER_MODEL=openrouter/free
```

---

## Project structure

```
/opt/proxmox-mcp/
├── .env                          # credentials (not committed)
├── .venv/                        # Python 3.12 virtual environment
├── server.py                     # MCP server — 46 Proxmox tools
├── agent.py                      # Interactive AI agent CLI
├── util.py                       # Metric formatting helpers
├── claude_desktop_config.json    # Ready-made Claude Desktop config
└── README.md                     # This file
```

---

## Proxmox API reference

Full API docs: <https://pve.proxmox.com/pve-docs/api-viewer/index.html>

| Tool | Proxmox API endpoint |
|------|---------------------|
| `list_nodes` | `GET /nodes` |
| `list_vms` | `GET /nodes/{node}/qemu` + `GET /nodes/{node}/lxc` |
| `list_storage` | `GET /storage` |
| `vm_status` | `GET /nodes/{node}/{type}/{vmid}/status/current` |
| `vm_config` | `GET /nodes/{node}/{type}/{vmid}/config` |
| `cluster_resources` | `GET /cluster/resources` |
| `cluster_tasks` | `GET /cluster/tasks` |
| `node_tasks` | `GET /nodes/{node}/tasks` |
| `list_snapshots` | `GET /nodes/{node}/{type}/{vmid}/snapshot` |
| `storage_content` | `GET /nodes/{node}/storage/{storage}/content` |
| `node_network` | `GET /nodes/{node}/network` |
| `list_backups` | `GET /nodes/{node}/storage/{storage}/content?content=backup` |
| `create_backup` | `POST /nodes/{node}/vzdump` |
| `restore_backup` | `POST /nodes/{node}/qemu` or `POST /nodes/{node}/lxc` |
| `vm_clone` | `POST /nodes/{node}/{type}/{vmid}/clone` |
| `vm_create` | `POST /nodes/{node}/qemu` or `POST /nodes/{node}/lxc` |
| `vm_delete` | `DELETE /nodes/{node}/{type}/{vmid}` |
| `vm_resize_disk` | `PUT /nodes/{node}/{type}/{vmid}/resize` |
| `list_firewall_rules` | `GET /cluster/firewall/rules` (or node/vm scope) |
| `create_firewall_rule` | `POST /cluster/firewall/rules` (or node/vm scope) |
| `delete_firewall_rule` | `DELETE /cluster/firewall/rules/{pos}` (or node/vm scope) |
| `list_firewall_aliases` | `GET /cluster/firewall/aliases` |
| `list_firewall_ipsets` | `GET /cluster/firewall/ipset` |
| `node_rrddata` | `GET /nodes/{node}/rrddata` |
| `vm_rrddata` | `GET /nodes/{node}/{type}/{vmid}/rrddata` |
| `cluster_status` | `GET /cluster/status` |
| `ha_resources` | `GET /cluster/ha/resources` |
| `ha_groups` | `GET /cluster/ha/groups` |
| `node_apt_updates` | `GET /nodes/{node}/apt/update` |
| `node_syslog` | `GET /nodes/{node}/syslog` |
| `node_dns` | `GET /nodes/{node}/dns` |
| `node_subscription` | `GET /nodes/{node}/subscription` |
| `list_users` | `GET /access/users` |
| `list_tokens` | `GET /access/users/{userid}/token` |
| `list_acl` | `GET /access/acl` |
| `list_pools` | `GET /pools` |
| `list_vnets` | `GET /sdn/vnets` |
| `list_sdn_zones` | `GET /sdn/zones` |
| `vm_start` | `POST /nodes/{node}/{type}/{vmid}/status/start` |
| `vm_stop` | `POST /nodes/{node}/{type}/{vmid}/status/stop` |
| `vm_shutdown` | `POST /nodes/{node}/{type}/{vmid}/status/shutdown` |
| `vm_reboot` | `POST /nodes/{node}/{type}/{vmid}/status/reboot` |
| `create_snapshot` | `POST /nodes/{node}/{type}/{vmid}/snapshot` |
| `delete_snapshot` | `DELETE /nodes/{node}/{type}/{vmid}/snapshot/{name}` |
| `rollback_snapshot` | `POST /nodes/{node}/{type}/{vmid}/snapshot/{name}/rollback` |
| `vm_migrate` | `POST /nodes/{node}/{type}/{vmid}/migrate` |
