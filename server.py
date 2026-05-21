"""
server.py — Proxmox MCP Server
================================
Implements a Model Context Protocol (MCP) server that exposes Proxmox VE
management capabilities as callable tools.  An AI agent (or any MCP client)
connects to this process over stdio and can query the cluster, inspect VMs /
containers, and trigger lifecycle actions such as start, stop, snapshot, and
live migration.

Transport  : stdio  (JSON-RPC 2.0 framing managed by the mcp library)
Protocol   : MCP 2024-11-05
Auth       : credentials loaded from .env via python-dotenv
API backend: proxmoxer (thin Python wrapper over the Proxmox VE REST API)

Tool categories (46 total)
---------------------------
  Informational — read-only (11)
    list_nodes, list_vms, list_storage, vm_status, vm_config,
    cluster_resources, cluster_tasks, node_tasks, list_snapshots,
    storage_content, node_network

  Backup (3)
    list_backups, create_backup, restore_backup

  Clone & provisioning (4)
    vm_clone, vm_create, vm_delete, vm_resize_disk

  Firewall (5)
    list_firewall_rules, create_firewall_rule, delete_firewall_rule,
    list_firewall_aliases, list_firewall_ipsets

  Historical metrics — RRD (2)
    node_rrddata, vm_rrddata

  High Availability (3)
    cluster_status, ha_resources, ha_groups

  Node — OS & system (4)
    node_apt_updates, node_syslog, node_dns, node_subscription

  Users & access control (4)
    list_users, list_tokens, list_acl, list_pools

  Software Defined Networking (2)
    list_vnets, list_sdn_zones

  Reversible lifecycle actions (4)
    vm_start, vm_stop, vm_shutdown, vm_reboot

  Persistent state changes ⚠ (4)
    create_snapshot, delete_snapshot, rollback_snapshot, vm_migrate

Authentication
--------------
  Two modes are supported — token auth takes priority if both sets of
  credentials are present.

  Password auth (basic):
    PROXMOX_USER        — API user including realm, e.g. root@pam
    PROXMOX_PASSWORD    — account password

  Token auth (recommended for automation):
    PROXMOX_USER        — API user that owns the token, e.g. root@pam
    PROXMOX_TOKEN_ID    — token name as shown in the Proxmox UI, e.g. mytoken
    PROXMOX_TOKEN_SECRET — token UUID value (shown once at token creation)

  Tokens are scoped, non-expiring by default, and do not require storing
  a user password.  Create one at Datacenter → Permissions → API Tokens.
  The token string used in HTTP headers is: "PVEAPIToken=user@realm!tokenid=uuid"

Environment variables (via .env)
---------------------------------
  PROXMOX_HOST           — hostname or IP of the Proxmox VE node
  PROXMOX_PORT           — API port (default 8006)
  PROXMOX_USER           — API user including realm (required for both auth modes)
  PROXMOX_PASSWORD       — password (password auth only)
  PROXMOX_TOKEN_ID       — token name (token auth only)
  PROXMOX_TOKEN_SECRET   — token UUID secret (token auth only)
  PROXMOX_VERIFY_SSL     — true/false, whether to verify TLS certificates (default false)
"""

import os
import asyncio
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from proxmoxer import ProxmoxAPI
import util

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()


def _build_proxmox_client() -> ProxmoxAPI:
    """
    Build and return a ProxmoxAPI session using the credentials from the
    environment.  Token authentication is used when PROXMOX_TOKEN_ID and
    PROXMOX_TOKEN_SECRET are both present; otherwise password authentication
    is used.

    Raises:
        EnvironmentError: if required variables are missing or the chosen
                          auth mode lacks its mandatory fields.
    """
    host = os.getenv("PROXMOX_HOST")
    port = os.getenv("PROXMOX_PORT", "8006")
    user = os.getenv("PROXMOX_USER")

    if not host:
        raise EnvironmentError("PROXMOX_HOST is not set.")
    if not user:
        raise EnvironmentError("PROXMOX_USER is not set.")

    # Parse PROXMOX_VERIFY_SSL — accepts "true"/"1"/"yes" (case-insensitive).
    # Defaults to False because most Proxmox installs use self-signed certs.
    verify_ssl_raw = os.getenv("PROXMOX_VERIFY_SSL", "false").strip().lower()
    verify_ssl = verify_ssl_raw in ("true", "1", "yes")

    token_id = os.getenv("PROXMOX_TOKEN_ID", "").strip()
    token_secret = os.getenv("PROXMOX_TOKEN_SECRET", "").strip()

    if token_id and token_secret:
        # Token authentication — does not require the user password.
        # proxmoxer sends the header:
        #   Authorization: PVEAPIToken=user@realm!tokenid=uuid-secret
        # Tokens can be scoped to specific privileges and are ideal for automation.
        return ProxmoxAPI(
            host,
            port=port,
            user=user,
            token_name=token_id,
            token_value=token_secret,
            verify_ssl=verify_ssl,
        )

    # Fall back to password authentication.
    password = os.getenv("PROXMOX_PASSWORD", "").strip()
    if not password:
        raise EnvironmentError(
            "No valid credentials found.  Set either "
            "PROXMOX_TOKEN_ID + PROXMOX_TOKEN_SECRET (recommended) "
            "or PROXMOX_PASSWORD."
        )
    return ProxmoxAPI(
        host,
        port=port,
        user=user,
        password=password,
        verify_ssl=verify_ssl,
    )


# Module-level client — built once at import time and reused across all tool calls.
proxmox = _build_proxmox_client()

server = Server("proxmox-mcp")

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

# JSON Schema properties shared by tools that target a specific VM or container.
_NODE_VMID_TYPE = {
    "node": {"type": "string", "description": "Proxmox node name."},
    "vmid": {"type": "integer", "description": "VM or container numeric ID (e.g. 100)."},
    "type": {
        "type": "string",
        "enum": ["qemu", "lxc"],
        "description": "'qemu' for KVM virtual machines, 'lxc' for Linux containers.",
    },
}
_REQUIRED_NVT = ["node", "vmid", "type"]

# JSON Schema properties shared by firewall tools that accept an optional scope.
_FIREWALL_SCOPE = {
    "level": {
        "type": "string",
        "enum": ["cluster", "node", "vm"],
        "description": (
            "Firewall scope: 'cluster' for cluster-wide rules, "
            "'node' for node-level rules (requires 'node'), "
            "'vm' for per-VM rules (requires node, vmid, type)."
        ),
    },
    "node": {"type": "string", "description": "Node name — required when level is 'node' or 'vm'."},
    "vmid": {"type": "integer", "description": "VM/CT ID — required when level is 'vm'."},
    "type": {"type": "string", "enum": ["qemu", "lxc"], "description": "VM type — required when level is 'vm'."},
}

# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """Return the full catalogue of tools exposed by this MCP server."""
    return [

        # ── Informational: read-only ───────────────────────────────────────

        types.Tool(
            name="list_nodes",
            description="List all Proxmox cluster nodes with status, CPU, memory, disk and uptime.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_vms",
            description="List all QEMU VMs and LXC containers on a node with live CPU and memory metrics.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="list_storage",
            description="List all cluster-level storage pools — type and supported content categories.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="vm_status",
            description="Live operational status of a VM or container: power state, CPU, memory, disk, uptime.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="vm_config",
            description="Full stored configuration of a VM or container: CPU, RAM, disks, network, boot order.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="cluster_resources",
            description="Unified view of every resource in the cluster (nodes, VMs, storage). Optional type filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["vm", "storage", "node", "sdn"],
                        "description": "Filter by resource type (optional).",
                    }
                },
            },
        ),
        types.Tool(
            name="cluster_tasks",
            description="Recent and currently running tasks across the entire cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="node_tasks",
            description="Recent and currently running tasks on a specific node.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "limit": {"type": "integer", "description": "Maximum tasks to return (default 50)."},
                },
                "required": ["node"],
            },
        ),
        types.Tool(
            name="list_snapshots",
            description="List snapshots of a VM or container with creation timestamps and descriptions.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="storage_content",
            description="List objects in a storage pool: ISO images, backups, disk images, templates.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node that owns the storage."},
                    "storage": {"type": "string", "description": "Storage pool name."},
                },
                "required": ["node", "storage"],
            },
        ),
        types.Tool(
            name="node_network",
            description="Network interface configuration of a node: NICs, bridges, bonds, VLANs.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),

        # ── Backup ────────────────────────────────────────────────────────

        types.Tool(
            name="list_backups",
            description=(
                "List all vzdump backup archives available in a storage pool on a node. "
                "Shows VM ID, creation time, size and backup format."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "storage": {"type": "string", "description": "Storage pool name (must support 'backup' content)."},
                },
                "required": ["node", "storage"],
            },
        ),
        types.Tool(
            name="create_backup",
            description=(
                "Start a vzdump backup job for a VM or container. "
                "Returns the task UPID that can be tracked via node_tasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node that hosts the VM/CT."},
                    "vmid": {"type": "integer", "description": "VM or container ID to back up."},
                    "storage": {"type": "string", "description": "Destination storage pool."},
                    "mode": {
                        "type": "string",
                        "enum": ["snapshot", "suspend", "stop"],
                        "description": "Backup mode (default: snapshot). Snapshot is live and preferred.",
                    },
                    "compress": {
                        "type": "string",
                        "enum": ["lzo", "gzip", "zstd"],
                        "description": "Compression algorithm (default: zstd).",
                    },
                },
                "required": ["node", "vmid", "storage"],
            },
        ),
        types.Tool(
            name="restore_backup",
            description=(
                "Restore a VM or container from a vzdump backup archive. "
                "The target vmid will be overwritten if it already exists (force=true). "
                "WARNING: existing data on the target vmid will be lost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node where the VM/CT will be restored."},
                    "vmid": {"type": "integer", "description": "Target VM/CT ID (will be created or overwritten)."},
                    "type": {"type": "string", "enum": ["qemu", "lxc"], "description": "Type of the backup."},
                    "volid": {"type": "string", "description": "Backup volume ID (e.g. local:backup/vzdump-qemu-100-....vma.zst)."},
                    "storage": {"type": "string", "description": "Storage pool for the restored VM's disks."},
                    "force": {"type": "boolean", "description": "Overwrite existing VM/CT with the same vmid (default false)."},
                },
                "required": ["node", "vmid", "type", "volid", "storage"],
            },
        ),

        # ── Clone & provisioning ──────────────────────────────────────────

        types.Tool(
            name="vm_clone",
            description=(
                "Clone an existing VM or container into a new one. "
                "For QEMU, supports linked clones (fast, require shared storage) "
                "and full clones (independent copy)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "newid": {"type": "integer", "description": "ID for the new cloned VM/CT."},
                    "name": {"type": "string", "description": "Name/hostname for the clone (optional)."},
                    "full": {"type": "boolean", "description": "Full clone — independent disk copy (QEMU only, default false = linked clone)."},
                    "target": {"type": "string", "description": "Destination node for the clone (optional, defaults to same node)."},
                },
                "required": [*_REQUIRED_NVT, "newid"],
            },
        ),
        types.Tool(
            name="vm_create",
            description=(
                "Create a new VM (QEMU) or container (LXC) from scratch. "
                "For QEMU: provide name, memory (MB), cores. "
                "For LXC: provide hostname, ostemplate (volid of a CT template), storage."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node where the VM/CT will be created."},
                    "vmid": {"type": "integer", "description": "ID for the new VM/CT."},
                    "type": {"type": "string", "enum": ["qemu", "lxc"], "description": "Resource type to create."},
                    "name": {"type": "string", "description": "VM name (QEMU) or container hostname (LXC)."},
                    "memory": {"type": "integer", "description": "RAM in MB (default 512)."},
                    "cores": {"type": "integer", "description": "Number of CPU cores (default 1)."},
                    "storage": {"type": "string", "description": "Storage pool for the root disk (LXC required, QEMU optional)."},
                    "ostemplate": {"type": "string", "description": "LXC only — volid of the OS template (e.g. local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst)."},
                    "disk_size": {"type": "string", "description": "Root disk size, e.g. '8G' (LXC only, default '8G')."},
                },
                "required": ["node", "vmid", "type"],
            },
        ),
        types.Tool(
            name="vm_delete",
            description=(
                "Permanently delete a VM or container and all its associated disk images. "
                "WARNING: this action is irreversible. The VM must be stopped first."
            ),
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="vm_resize_disk",
            description=(
                "Extend a disk attached to a VM or container. "
                "Size can be absolute (e.g. '50G') or relative (e.g. '+10G'). "
                "Disk shrinking is not supported by Proxmox."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "disk": {"type": "string", "description": "Disk identifier, e.g. 'scsi0', 'virtio0', 'rootfs'."},
                    "size": {"type": "string", "description": "New size (e.g. '50G') or increment (e.g. '+10G')."},
                },
                "required": [*_REQUIRED_NVT, "disk", "size"],
            },
        ),

        # ── Firewall ──────────────────────────────────────────────────────

        types.Tool(
            name="list_firewall_rules",
            description=(
                "List firewall rules at cluster, node or VM level. "
                "Set level='cluster' for cluster-wide rules, 'node' for a specific node, "
                "'vm' for a specific VM or container."
            ),
            inputSchema={
                "type": "object",
                "properties": _FIREWALL_SCOPE,
                "required": ["level"],
            },
        ),
        types.Tool(
            name="create_firewall_rule",
            description=(
                "Add a firewall rule at cluster, node or VM level. "
                "Rules are appended at the end of the list (highest position index)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_FIREWALL_SCOPE,
                    "action": {
                        "type": "string",
                        "enum": ["ACCEPT", "DROP", "REJECT"],
                        "description": "Rule action.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["in", "out"],
                        "description": "Traffic direction.",
                    },
                    "proto": {"type": "string", "description": "Protocol, e.g. 'tcp', 'udp', 'icmp' (optional)."},
                    "source": {"type": "string", "description": "Source IP/CIDR or alias (optional)."},
                    "dest": {"type": "string", "description": "Destination IP/CIDR or alias (optional)."},
                    "dport": {"type": "string", "description": "Destination port or range, e.g. '80' or '8000:9000' (optional)."},
                    "sport": {"type": "string", "description": "Source port or range (optional)."},
                    "comment": {"type": "string", "description": "Human-readable rule description (optional)."},
                    "enable": {"type": "integer", "enum": [0, 1], "description": "1 to enable the rule immediately (default 1)."},
                },
                "required": ["level", "action", "direction"],
            },
        ),
        types.Tool(
            name="delete_firewall_rule",
            description=(
                "Delete a firewall rule by its position index at cluster, node or VM level. "
                "Use list_firewall_rules to find the rule position."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_FIREWALL_SCOPE,
                    "pos": {"type": "integer", "description": "Zero-based position index of the rule to delete."},
                },
                "required": ["level", "pos"],
            },
        ),
        types.Tool(
            name="list_firewall_aliases",
            description="List all IP aliases defined in the cluster firewall (named IP addresses or CIDRs).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_firewall_ipsets",
            description="List all IP sets defined in the cluster firewall (named groups of IP addresses).",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Historical metrics — RRD ──────────────────────────────────────

        types.Tool(
            name="node_rrddata",
            description=(
                "Retrieve historical performance metrics for a node: CPU, memory, "
                "network I/O and disk I/O averaged over a selected time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "timeframe": {
                        "type": "string",
                        "enum": ["hour", "day", "week", "month", "year"],
                        "description": "Time window to return (default: hour).",
                    },
                },
                "required": ["node"],
            },
        ),
        types.Tool(
            name="vm_rrddata",
            description=(
                "Retrieve historical performance metrics for a VM or container: CPU, memory, "
                "network I/O and disk I/O averaged over a selected time window."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "timeframe": {
                        "type": "string",
                        "enum": ["hour", "day", "week", "month", "year"],
                        "description": "Time window to return (default: hour).",
                    },
                },
                "required": _REQUIRED_NVT,
            },
        ),

        # ── High Availability ─────────────────────────────────────────────

        types.Tool(
            name="cluster_status",
            description="Proxmox cluster quorum status and HA manager state.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="ha_resources",
            description="List all resources managed by the Proxmox HA manager with their current state.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="ha_groups",
            description="List all HA groups and their node priority assignments.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Node — OS & system ────────────────────────────────────────────

        types.Tool(
            name="node_apt_updates",
            description="List available APT package updates on a Proxmox node.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_syslog",
            description="Retrieve the most recent system log entries from a Proxmox node.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "limit": {"type": "integer", "description": "Number of log lines to return (default 100)."},
                },
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_dns",
            description="Retrieve the DNS resolver configuration (nameservers and search domain) of a node.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_subscription",
            description="Check the Proxmox VE subscription status of a node (active, expired, none).",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),

        # ── Users & access control ────────────────────────────────────────

        types.Tool(
            name="list_users",
            description="List all Proxmox VE users with their realm, enabled status and expiry date.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_tokens",
            description="List API tokens associated with a specific Proxmox user.",
            inputSchema={
                "type": "object",
                "properties": {
                    "userid": {
                        "type": "string",
                        "description": "Full user ID including realm, e.g. 'root@pam' or 'admin@pve'.",
                    }
                },
                "required": ["userid"],
            },
        ),
        types.Tool(
            name="list_acl",
            description="List all Access Control List entries — who can do what on which resource path.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_pools",
            description="List all resource pools with their member VMs and storage pools.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Software Defined Networking ───────────────────────────────────

        types.Tool(
            name="list_vnets",
            description="List all SDN virtual networks (VNets) defined in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_sdn_zones",
            description="List all SDN zones (VXLAN, EVPN, Simple, QinQ, etc.) defined in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Reversible lifecycle actions ──────────────────────────────────

        types.Tool(
            name="vm_start",
            description="Power on a stopped VM or container.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="vm_stop",
            description="Force power off a VM or container (equivalent to pulling the power plug).",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="vm_shutdown",
            description="Send an ACPI shutdown signal for a graceful OS shutdown.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),
        types.Tool(
            name="vm_reboot",
            description="Reboot a running VM or container.",
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),

        # ── Persistent state changes ⚠ ────────────────────────────────────

        types.Tool(
            name="create_snapshot",
            description="Create a point-in-time snapshot of a VM or container.",
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "name": {"type": "string", "description": "Snapshot name (alphanumeric, no spaces)."},
                    "description": {"type": "string", "description": "Optional description."},
                },
                "required": [*_REQUIRED_NVT, "name"],
            },
        ),
        types.Tool(
            name="delete_snapshot",
            description="Permanently delete a snapshot. Irreversible.",
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "name": {"type": "string", "description": "Snapshot name to delete."},
                },
                "required": [*_REQUIRED_NVT, "name"],
            },
        ),
        types.Tool(
            name="rollback_snapshot",
            description="Restore a VM or container to a snapshot state. All changes since the snapshot are lost.",
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "name": {"type": "string", "description": "Snapshot name to restore."},
                },
                "required": [*_REQUIRED_NVT, "name"],
            },
        ),
        types.Tool(
            name="vm_migrate",
            description="Migrate a VM or container to another cluster node (offline or live).",
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "target": {"type": "string", "description": "Destination node name."},
                    "online": {"type": "boolean", "description": "Live migration without downtime (QEMU only)."},
                },
                "required": [*_REQUIRED_NVT, "target"],
            },
        ),
    ]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vm_api(node: str, vmid: int, vm_type: str):
    """
    Return the proxmoxer sub-resource for a specific VM or container.

    Centralises the qemu/lxc branching so every handler can call
    _vm_api(node, vmid, type).status.current.get() without repeating if/else.
    """
    if vm_type == "qemu":
        return proxmox.nodes(node).qemu(vmid)
    return proxmox.nodes(node).lxc(vmid)


def _firewall_api(level: str, node: str = None, vmid: int = None, vm_type: str = None):
    """
    Return the proxmoxer firewall sub-resource for the requested scope.

    Proxmox exposes firewall rules at three independent levels:
      cluster  →  /cluster/firewall
      node     →  /nodes/{node}/firewall
      vm       →  /nodes/{node}/{type}/{vmid}/firewall

    Args:
        level   : "cluster", "node", or "vm"
        node    : required for "node" and "vm"
        vmid    : required for "vm"
        vm_type : required for "vm"
    """
    if level == "cluster":
        return proxmox.cluster.firewall
    if level == "node":
        return proxmox.nodes(node).firewall
    # level == "vm"
    return _vm_api(node, vmid, vm_type).firewall


def _fmt_rrd(data: list) -> str:
    """
    Format RRD time-series data into a readable table.

    RRD data is a list of dicts, each representing one time bucket.
    Keys vary by resource type but typically include: time, cpu, mem,
    netin, netout, diskread, diskwrite, maxmem, maxcpu.
    We display the last 10 data points to keep the output concise.
    """
    if not data:
        return "No RRD data available."

    # Use the last 10 samples — most recent data is most relevant.
    sample = data[-10:]
    lines = []
    for point in sample:
        parts = []
        if "time" in point:
            import datetime
            ts = datetime.datetime.fromtimestamp(point["time"]).strftime("%Y-%m-%d %H:%M")
            parts.append(f"time={ts}")
        if "cpu" in point and point["cpu"] is not None:
            parts.append(f"cpu={util.decimaltopercentage(point['cpu'])}")
        if "mem" in point and point["mem"] is not None:
            parts.append(f"mem={util.bytes_to_human_readable(int(point['mem']))}")
        if "netin" in point and point["netin"] is not None:
            parts.append(f"netin={util.bytes_to_human_readable(int(point['netin']))}/s")
        if "netout" in point and point["netout"] is not None:
            parts.append(f"netout={util.bytes_to_human_readable(int(point['netout']))}/s")
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """
    Dispatch an incoming tool call to the appropriate Proxmox API operation.

    Returns a list with a single TextContent block.  Raises ValueError for
    unknown tool names (should never happen in normal operation).
    """

    # ── Informational: read-only ───────────────────────────────────────────

    if name == "list_nodes":
        # GET /nodes — cluster-level node list with live metrics.
        nodes = proxmox.nodes.get()
        lines = [
            f"Node: {n['node']} | {n['status']} | "
            f"CPU: {util.decimaltopercentage(n['cpu'])} | "
            f"Memory: {util.bytes_to_human_readable(n['mem'])}/{util.bytes_to_human_readable(n['maxmem'])} | "
            f"Disk: {util.bytes_to_human_readable(n['disk'])}/{util.bytes_to_human_readable(n['maxdisk'])} | "
            f"Uptime: {util.second_to_human_readable(n['uptime'])}"
            for n in nodes
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "list_vms":
        node = arguments["node"]
        lines = []
        for vm in proxmox.nodes(node).qemu.get():
            lines.append(
                f"[QEMU] {vm['vmid']}. {vm['name']} => {vm['status']} | "
                f"CPU: {util.decimaltopercentage(vm.get('cpu', 0))} | "
                f"Memory: {util.bytes_to_human_readable(vm.get('mem', 0))}"
                f"/{util.bytes_to_human_readable(vm.get('maxmem', 0))}"
            )
        for ct in proxmox.nodes(node).lxc.get():
            lines.append(
                f"[LXC]  {ct['vmid']}. {ct['name']} => {ct['status']} | "
                f"CPU: {util.decimaltopercentage(ct.get('cpu', 0))} | "
                f"Memory: {util.bytes_to_human_readable(ct.get('mem', 0))}"
                f"/{util.bytes_to_human_readable(ct.get('maxmem', 0))}"
            )
        return [types.TextContent(type="text", text="\n".join(lines) or "No VMs or containers found.")]

    if name == "list_storage":
        storages = proxmox.storage.get()
        lines = [f"Storage: {s['storage']} | Type: {s['type']} | Content: {s['content']}" for s in storages]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "vm_status":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        # GET /nodes/{node}/{type}/{vmid}/status/current — live sampled metrics.
        status = _vm_api(node, vmid, vm_type).status.current.get()
        lines = [
            f"Name:   {status.get('name', vmid)}",
            f"Status: {status['status']}",
            f"CPU:    {util.decimaltopercentage(status.get('cpu', 0))}",
            f"Memory: {util.bytes_to_human_readable(status.get('mem', 0))}/{util.bytes_to_human_readable(status.get('maxmem', 0))}",
            f"Disk:   {util.bytes_to_human_readable(status.get('disk', 0))}/{util.bytes_to_human_readable(status.get('maxdisk', 0))}",
            f"Uptime: {util.second_to_human_readable(status.get('uptime', 0))}",
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "vm_config":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        # GET /nodes/{node}/{type}/{vmid}/config — stored configuration, sorted alphabetically.
        config = _vm_api(node, vmid, vm_type).config.get()
        lines = [f"{k}: {v}" for k, v in sorted(config.items())]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "cluster_resources":
        params = {}
        if "type" in arguments:
            params["type"] = arguments["type"]
        resources = proxmox.cluster.resources.get(**params)
        lines = []
        for r in resources:
            parts = [f"{r.get('type', '?')}: {r.get('name', r.get('storage', r.get('id', '?')))}"]
            if "status" in r:
                parts.append(f"status={r['status']}")
            if "cpu" in r:
                parts.append(f"CPU={util.decimaltopercentage(r['cpu'])}")
            if "mem" in r and "maxmem" in r:
                parts.append(f"Mem={util.bytes_to_human_readable(r['mem'])}/{util.bytes_to_human_readable(r['maxmem'])}")
            if "node" in r:
                parts.append(f"node={r['node']}")
            lines.append(" | ".join(parts))
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "cluster_tasks":
        tasks = proxmox.cluster.tasks.get()
        lines = [
            f"{t.get('starttime', '')} | {t.get('node', '?')} | {t.get('type', '?')} | "
            f"VMID={t.get('id', '?')} | {t.get('status', '?')} | user={t.get('user', '?')}"
            for t in tasks
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No tasks found.")]

    if name == "node_tasks":
        node = arguments["node"]
        limit = arguments.get("limit", 50)
        tasks = proxmox.nodes(node).tasks.get(limit=limit)
        lines = [
            f"{t.get('starttime', '')} | {t.get('type', '?')} | "
            f"VMID={t.get('id', '?')} | {t.get('status', '?')} | user={t.get('user', '?')}"
            for t in tasks
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No tasks found.")]

    if name == "list_snapshots":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        snapshots = _vm_api(node, vmid, vm_type).snapshot.get()
        # "current" is a Proxmox pseudo-snapshot representing live state — exclude it.
        lines = [
            f"{s['name']} | {s.get('description', '')} | snaptime={s.get('snaptime', 'N/A')}"
            for s in snapshots if s["name"] != "current"
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No snapshots found.")]

    if name == "storage_content":
        node, storage = arguments["node"], arguments["storage"]
        items = proxmox.nodes(node).storage(storage).content.get()
        lines = [
            f"{i.get('content', '?')} | {i.get('volid', '?')} | "
            f"size={util.bytes_to_human_readable(i.get('size', 0))}"
            for i in items
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "Storage is empty.")]

    if name == "node_network":
        node = arguments["node"]
        interfaces = proxmox.nodes(node).network.get()
        lines = [
            f"{i['iface']} | type={i.get('type', '?')} | "
            f"address={i.get('address', 'N/A')} | active={i.get('active', 0)}"
            for i in interfaces
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── Backup ─────────────────────────────────────────────────────────────

    if name == "list_backups":
        node, storage = arguments["node"], arguments["storage"]
        # GET /nodes/{node}/storage/{storage}/content?content=backup
        # Returns only backup archives (vzdump .vma / .tar files).
        items = proxmox.nodes(node).storage(storage).content.get(content="backup")
        lines = [
            f"VMID={i.get('vmid', '?')} | {i.get('volid', '?')} | "
            f"size={util.bytes_to_human_readable(i.get('size', 0))} | "
            f"ctime={i.get('ctime', 'N/A')} | format={i.get('format', '?')}"
            for i in items
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No backups found.")]

    if name == "create_backup":
        node = arguments["node"]
        vmid = arguments["vmid"]
        storage = arguments["storage"]
        # POST /nodes/{node}/vzdump
        # mode defaults to 'snapshot' (live backup, no downtime for QEMU with QEMU agent).
        # compress defaults to 'zstd' (fast, good ratio, native Proxmox default since PVE 7).
        params = {
            "vmid": vmid,
            "storage": storage,
            "mode": arguments.get("mode", "snapshot"),
            "compress": arguments.get("compress", "zstd"),
        }
        result = proxmox.nodes(node).vzdump.post(**params)
        return [types.TextContent(type="text", text=f"Backup task queued: {result}")]

    if name == "restore_backup":
        node = arguments["node"]
        vmid = arguments["vmid"]
        vm_type = arguments["type"]
        volid = arguments["volid"]
        storage = arguments["storage"]
        force = 1 if arguments.get("force") else 0

        if vm_type == "qemu":
            # POST /nodes/{node}/qemu — restores a QEMU backup archive.
            # 'archive' is the vzdump volume ID.  'force' allows overwriting an existing vmid.
            result = proxmox.nodes(node).qemu.post(
                vmid=vmid, archive=volid, storage=storage, force=force
            )
        else:
            # POST /nodes/{node}/lxc — restores an LXC backup.
            # 'restore=1' tells Proxmox this is a restore, not a fresh container creation.
            result = proxmox.nodes(node).lxc.post(
                vmid=vmid, ostemplate=volid, storage=storage, restore=1, force=force
            )
        return [types.TextContent(type="text", text=f"Restore task queued: {result}")]

    # ── Clone & provisioning ───────────────────────────────────────────────

    if name == "vm_clone":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        newid = arguments["newid"]
        # POST /nodes/{node}/{type}/{vmid}/clone
        params = {"newid": newid}
        if "name" in arguments:
            # QEMU uses 'name'; LXC uses 'hostname' — proxmoxer passes both and
            # the API silently ignores the irrelevant one.
            params["name"] = arguments["name"]
            params["hostname"] = arguments["name"]
        if "full" in arguments and vm_type == "qemu":
            # Full clone: independent copy of every disk image.
            # Linked clone (full=0): shares base snapshots — faster but requires shared storage.
            params["full"] = 1 if arguments["full"] else 0
        if "target" in arguments:
            params["target"] = arguments["target"]
        result = _vm_api(node, vmid, vm_type).clone.post(**params)
        return [types.TextContent(type="text", text=f"Clone task queued: {result}")]

    if name == "vm_create":
        node = arguments["node"]
        vmid = arguments["vmid"]
        vm_type = arguments["type"]
        memory = arguments.get("memory", 512)   # MB
        cores = arguments.get("cores", 1)

        if vm_type == "qemu":
            # POST /nodes/{node}/qemu — creates a new empty QEMU VM.
            # Without a disk or ISO, the VM boots into PXE.  The caller can
            # attach storage separately or via vm_config changes.
            params = {
                "vmid": vmid,
                "name": arguments.get("name", f"vm-{vmid}"),
                "memory": memory,
                "cores": cores,
                "scsihw": "virtio-scsi-pci",   # modern SCSI controller, required for hot-plug
                "net0": "virtio,bridge=vmbr0",  # default network interface on the default bridge
            }
            if "storage" in arguments:
                # Attach a basic virtio disk if storage is specified.
                disk_size = arguments.get("disk_size", "8G")
                params["scsi0"] = f"{arguments['storage']}:{disk_size.rstrip('G')},format=qcow2"
            result = proxmox.nodes(node).qemu.post(**params)

        else:
            # POST /nodes/{node}/lxc — creates a new LXC container.
            # ostemplate is required (volume ID of a downloaded CT template).
            params = {
                "vmid": vmid,
                "hostname": arguments.get("name", f"ct-{vmid}"),
                "memory": memory,
                "cores": cores,
                "ostemplate": arguments.get("ostemplate", ""),
                "storage": arguments.get("storage", "local"),
                "rootfs": f"{arguments.get('storage', 'local')}:{arguments.get('disk_size', '8').rstrip('G')}",
                "net0": "name=eth0,bridge=vmbr0,ip=dhcp",   # DHCP on the default bridge
            }
            result = proxmox.nodes(node).lxc.post(**params)

        return [types.TextContent(type="text", text=f"Create task queued: {result}")]

    if name == "vm_delete":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        # DELETE /nodes/{node}/{type}/{vmid}
        # Proxmox requires the VM to be stopped.  purge=1 would also remove backup jobs
        # and HA configuration, but we keep it conservative here.
        result = _vm_api(node, vmid, vm_type).delete()
        return [types.TextContent(type="text", text=f"Delete task queued: {result}")]

    if name == "vm_resize_disk":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        disk = arguments["disk"]
        size = arguments["size"]
        # PUT /nodes/{node}/{type}/{vmid}/resize
        # 'size' accepts absolute (e.g. "50G") or relative (e.g. "+10G") values.
        # Proxmox does not support shrinking — the API will reject a smaller absolute size.
        result = _vm_api(node, vmid, vm_type).resize.put(disk=disk, size=size)
        return [types.TextContent(type="text", text=f"Disk resize task queued: {result}")]

    # ── Firewall ───────────────────────────────────────────────────────────

    if name == "list_firewall_rules":
        level = arguments["level"]
        fw = _firewall_api(
            level,
            node=arguments.get("node"),
            vmid=arguments.get("vmid"),
            vm_type=arguments.get("type"),
        )
        rules = fw.rules.get()
        lines = [
            f"pos={r.get('pos', '?')} | {r.get('type', '?')} | {r.get('action', '?')} | "
            f"{r.get('macro', r.get('proto', '?'))} | "
            f"src={r.get('source', 'any')} → dst={r.get('dest', 'any')} | "
            f"dport={r.get('dport', 'any')} | enable={r.get('enable', 1)} | {r.get('comment', '')}"
            for r in rules
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No firewall rules found.")]

    if name == "create_firewall_rule":
        level = arguments["level"]
        fw = _firewall_api(
            level,
            node=arguments.get("node"),
            vmid=arguments.get("vmid"),
            vm_type=arguments.get("type"),
        )
        # Build the rule parameters — only include keys that were provided.
        params = {
            "action": arguments["action"],
            "type": arguments["direction"],   # Proxmox names the field 'type' (in/out/forward)
            "enable": arguments.get("enable", 1),
        }
        for opt in ("proto", "source", "dest", "dport", "sport", "comment"):
            if opt in arguments:
                params[opt] = arguments[opt]
        fw.rules.post(**params)
        return [types.TextContent(type="text", text="Firewall rule created.")]

    if name == "delete_firewall_rule":
        level = arguments["level"]
        pos = arguments["pos"]
        fw = _firewall_api(
            level,
            node=arguments.get("node"),
            vmid=arguments.get("vmid"),
            vm_type=arguments.get("type"),
        )
        # DELETE /...firewall/rules/{pos} — pos is the zero-based rule index.
        fw.rules(pos).delete()
        return [types.TextContent(type="text", text=f"Firewall rule at position {pos} deleted.")]

    if name == "list_firewall_aliases":
        # GET /cluster/firewall/aliases — cluster-wide named IP aliases.
        aliases = proxmox.cluster.firewall.aliases.get()
        lines = [f"{a['name']} = {a['cidr']} | {a.get('comment', '')}" for a in aliases]
        return [types.TextContent(type="text", text="\n".join(lines) or "No aliases defined.")]

    if name == "list_firewall_ipsets":
        # GET /cluster/firewall/ipset — named groups of IPs/CIDRs used in firewall rules.
        ipsets = proxmox.cluster.firewall.ipset.get()
        lines = [f"{s['name']} | {s.get('comment', '')}" for s in ipsets]
        return [types.TextContent(type="text", text="\n".join(lines) or "No IP sets defined.")]

    # ── Historical metrics — RRD ───────────────────────────────────────────

    if name == "node_rrddata":
        node = arguments["node"]
        timeframe = arguments.get("timeframe", "hour")
        # GET /nodes/{node}/rrddata?timeframe={tf}&cf=AVERAGE
        # cf=AVERAGE returns time-bucket averages (vs MAX or LAST).
        data = proxmox.nodes(node).rrddata.get(timeframe=timeframe, cf="AVERAGE")
        return [types.TextContent(type="text", text=_fmt_rrd(data))]

    if name == "vm_rrddata":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        timeframe = arguments.get("timeframe", "hour")
        # GET /nodes/{node}/{type}/{vmid}/rrddata?timeframe={tf}&cf=AVERAGE
        data = _vm_api(node, vmid, vm_type).rrddata.get(timeframe=timeframe, cf="AVERAGE")
        return [types.TextContent(type="text", text=_fmt_rrd(data))]

    # ── High Availability ──────────────────────────────────────────────────

    if name == "cluster_status":
        # GET /cluster/status — returns quorum status and HA manager state.
        # Each entry has a 'type' of either 'cluster' or 'node'.
        items = proxmox.cluster.status.get()
        lines = [
            f"{i.get('type', '?')}: {i.get('name', '?')} | "
            f"online={i.get('online', '?')} | quorate={i.get('quorate', 'N/A')} | "
            f"nodes={i.get('nodes', 'N/A')}"
            for i in items
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "ha_resources":
        # GET /cluster/ha/resources — resources tracked by the HA manager.
        resources = proxmox.cluster.ha.resources.get()
        lines = [
            f"{r.get('sid', '?')} | state={r.get('state', '?')} | "
            f"group={r.get('group', 'none')} | max_restart={r.get('max_restart', '?')}"
            for r in resources
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No HA resources configured.")]

    if name == "ha_groups":
        # GET /cluster/ha/groups — HA groups define which nodes can host a resource.
        groups = proxmox.cluster.ha.groups.get()
        lines = [
            f"{g.get('group', '?')} | nodes={g.get('nodes', '?')} | "
            f"restricted={g.get('restricted', 0)} | nofailback={g.get('nofailback', 0)}"
            for g in groups
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No HA groups configured.")]

    # ── Node — OS & system ─────────────────────────────────────────────────

    if name == "node_apt_updates":
        node = arguments["node"]
        # GET /nodes/{node}/apt/update — lists packages with available upgrades.
        # Returns: package name, current version, new version, priority.
        updates = proxmox.nodes(node).apt.update.get()
        lines = [
            f"{u.get('Package', '?')} | {u.get('OldVersion', '?')} → {u.get('Version', '?')} | "
            f"priority={u.get('Priority', '?')}"
            for u in updates
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "System is up to date.")]

    if name == "node_syslog":
        node = arguments["node"]
        limit = arguments.get("limit", 100)
        # GET /nodes/{node}/syslog?limit={limit} — returns the most recent log lines.
        # Each entry is a dict with 'n' (line number) and 't' (text).
        entries = proxmox.nodes(node).syslog.get(limit=limit)
        lines = [e.get("t", "") for e in entries]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "node_dns":
        node = arguments["node"]
        # GET /nodes/{node}/dns — returns dns1, dns2, dns3, search fields.
        dns = proxmox.nodes(node).dns.get()
        lines = [
            f"search={dns.get('search', 'N/A')}",
            f"dns1={dns.get('dns1', 'N/A')}",
            f"dns2={dns.get('dns2', 'N/A')}",
            f"dns3={dns.get('dns3', 'N/A')}",
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "node_subscription":
        node = arguments["node"]
        # GET /nodes/{node}/subscription — returns status, level, product key info.
        sub = proxmox.nodes(node).subscription.get()
        lines = [
            f"status={sub.get('status', 'N/A')}",
            f"level={sub.get('level', 'N/A')}",
            f"product={sub.get('product', 'N/A')}",
            f"key={sub.get('key', 'N/A')}",
            f"next_due={sub.get('nextduedate', 'N/A')}",
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── Users & access control ─────────────────────────────────────────────

    if name == "list_users":
        # GET /access/users — all users defined in the Proxmox access database.
        users = proxmox.access.users.get()
        lines = [
            f"{u.get('userid', '?')} | enabled={u.get('enable', 1)} | "
            f"expire={u.get('expire', 0) or 'never'} | comment={u.get('comment', '')}"
            for u in users
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "list_tokens":
        userid = arguments["userid"]
        # GET /access/users/{userid}/token — API tokens for a specific user.
        # Tokens are used for automation without exposing the user password.
        tokens = proxmox.access.users(userid).token.get()
        lines = [
            f"{t.get('tokenid', '?')} | expire={t.get('expire', 0) or 'never'} | "
            f"privsep={t.get('privsep', 1)} | comment={t.get('comment', '')}"
            for t in tokens
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No tokens found.")]

    if name == "list_acl":
        # GET /access/acl — flat list of all ACL entries in the cluster.
        # Each entry: path (resource), type (user/group/token), ugid, roleid, propagate.
        acl = proxmox.access.acl.get()
        lines = [
            f"path={a.get('path', '?')} | {a.get('type', '?')}={a.get('ugid', '?')} | "
            f"role={a.get('roleid', '?')} | propagate={a.get('propagate', 0)}"
            for a in acl
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No ACL entries found.")]

    if name == "list_pools":
        # GET /pools — resource pool objects.
        # Each pool groups VMs and storage for easier permission management.
        pools = proxmox.pools.get()
        lines = []
        for pool in pools:
            # GET /pools/{poolid} for member details.
            detail = proxmox.pools(pool["poolid"]).get()
            members = detail.get("members", [])
            vm_ids = [str(m.get("vmid", m.get("storage", "?"))) for m in members]
            lines.append(
                f"{pool['poolid']} | comment={pool.get('comment', '')} | "
                f"members=[{', '.join(vm_ids)}]"
            )
        return [types.TextContent(type="text", text="\n".join(lines) or "No pools defined.")]

    # ── Software Defined Networking ────────────────────────────────────────

    if name == "list_vnets":
        # GET /sdn/vnets — virtual networks in the SDN fabric.
        # Each VNet belongs to a zone and may have a VLAN or VNI tag.
        try:
            vnets = proxmox.sdn.vnets.get()
            lines = [
                f"{v.get('vnet', '?')} | zone={v.get('zone', '?')} | "
                f"tag={v.get('tag', 'N/A')} | alias={v.get('alias', '')}"
                for v in vnets
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No VNets defined.")]
        except Exception as e:
            # SDN may not be configured or the user may lack permission.
            return [types.TextContent(type="text", text=f"SDN not available: {e}")]

    if name == "list_sdn_zones":
        # GET /sdn/zones — SDN zone definitions (Simple, VXLAN, EVPN, QinQ).
        try:
            zones = proxmox.sdn.zones.get()
            lines = [
                f"{z.get('zone', '?')} | type={z.get('type', '?')} | "
                f"nodes={z.get('nodes', 'all')} | mtu={z.get('mtu', 'default')}"
                for z in zones
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No SDN zones defined.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"SDN not available: {e}")]

    # ── Reversible lifecycle actions ───────────────────────────────────────

    if name in ("vm_start", "vm_stop", "vm_shutdown", "vm_reboot"):
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        # Extract the action verb: start | stop | shutdown | reboot
        # POST /nodes/{node}/{type}/{vmid}/status/{action}
        action = name.split("_", 1)[1]
        result = _vm_api(node, vmid, vm_type).status(action).post()
        return [types.TextContent(type="text", text=f"Task queued: {result}")]

    # ── Persistent state changes ───────────────────────────────────────────

    if name == "create_snapshot":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        params = {"snapname": arguments["name"]}
        if "description" in arguments:
            params["description"] = arguments["description"]
        result = _vm_api(node, vmid, vm_type).snapshot.post(**params)
        return [types.TextContent(type="text", text=f"Snapshot task queued: {result}")]

    if name == "delete_snapshot":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        result = _vm_api(node, vmid, vm_type).snapshot(arguments["name"]).delete()
        return [types.TextContent(type="text", text=f"Delete snapshot task queued: {result}")]

    if name == "rollback_snapshot":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        result = _vm_api(node, vmid, vm_type).snapshot(arguments["name"]).rollback.post()
        return [types.TextContent(type="text", text=f"Rollback task queued: {result}")]

    if name == "vm_migrate":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        params = {"target": arguments["target"]}
        if arguments.get("online"):
            params["online"] = 1   # Proxmox expects integer 1, not boolean True
        result = _vm_api(node, vmid, vm_type).migrate.post(**params)
        return [types.TextContent(type="text", text=f"Migration task queued: {result}")]

    raise ValueError(f"Unknown tool: {name}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    """Start the MCP server on stdio and block until the streams close."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
