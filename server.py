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

Tool categories (91 total)
---------------------------
  Informational — read-only (11)
    list_nodes, list_vms, list_storage, vm_status, vm_config,
    cluster_resources, cluster_tasks, node_tasks, list_snapshots,
    storage_content, node_network

  Backup (3)
    list_backups, create_backup, restore_backup

  Clone & provisioning (4)
    vm_clone, vm_create, vm_delete, vm_resize_disk

  Disk management (4)
    vm_move_disk, vm_unlink_disk, list_node_disks, vm_template

  Firewall (5)
    list_firewall_rules, create_firewall_rule, delete_firewall_rule,
    list_firewall_aliases, list_firewall_ipsets

  Historical metrics — RRD (2)
    node_rrddata, vm_rrddata

  High Availability (3)
    cluster_status, ha_resources, ha_groups

  QEMU Guest Agent (3)
    vm_agent_exec, vm_agent_info, vm_agent_network

  LXC exec (1)
    lxc_exec

  Backup jobs (2)
    list_backup_jobs, prune_backups

  Replication (3)
    list_replication, create_replication, delete_replication

  Ceph (4)
    ceph_status, ceph_health, ceph_osds, ceph_pools

  Node — OS & system (8)
    node_apt_updates, node_syslog, node_dns, node_subscription,
    node_reboot, node_shutdown, node_apt_upgrade, node_certificates

  Users & access control (4)
    list_users, list_tokens, list_acl, list_pools

  Software Defined Networking (2)
    list_vnets, list_sdn_zones

  Notifications (2)
    list_notification_endpoints, list_notification_matchers

  Console (1)
    vm_console_url

  Reversible lifecycle actions (4)
    vm_start, vm_stop, vm_shutdown, vm_reboot

  Persistent state changes ⚠ (4)
    create_snapshot, delete_snapshot, rollback_snapshot, vm_migrate

  Task management (2)
    wait_for_task, cancel_task

  VM / CT configuration write (2)
    vm_set_config, vm_set_cdrom

  Storage management (3)
    storage_status, create_storage, delete_storage

  Resource pool management (4)
    create_pool, delete_pool, pool_add_member, pool_remove_member

  Network management (3)
    create_network, delete_network, apply_network_config

  Diagnostics (3)
    node_smart, cluster_health_summary, node_top

  ACME / TLS certificates (2)
    list_acme_accounts, renew_certificate

  API token management (2)
    create_api_token, delete_api_token

Authentication
--------------
  Two modes are supported — token auth takes priority if both sets of
  credentials are present.

  Password auth (basic):
    PROXMOX_MCP_USER        — API user including realm, e.g. root@pam
    PROXMOX_MCP_PASSWORD    — account password

  Token auth (recommended for automation):
    PROXMOX_MCP_USER        — API user that owns the token, e.g. root@pam
    PROXMOX_MCP_TOKEN_ID    — token name as shown in the Proxmox UI, e.g. mytoken
    PROXMOX_MCP_TOKEN_SECRET — token UUID value (shown once at token creation)

  Tokens are scoped, non-expiring by default, and do not require storing
  a user password.  Create one at Datacenter → Permissions → API Tokens.
  The token string used in HTTP headers is: "PVEAPIToken=user@realm!tokenid=uuid"

Environment variables (via .env)
---------------------------------
  PROXMOX_MCP_HOST           — hostname or IP of the Proxmox VE node
  PROXMOX_MCP_PORT           — API port (default 8006)
  PROXMOX_MCP_USER           — API user including realm (required for both auth modes)
  PROXMOX_MCP_PASSWORD       — password (password auth only)
  PROXMOX_MCP_TOKEN_ID       — token name (token auth only)
  PROXMOX_MCP_TOKEN_SECRET   — token UUID secret (token auth only)
  PROXMOX_MCP_VERIFY_SSL     — true/false, whether to verify TLS certificates (default false)
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
    environment.  Token authentication is used when PROXMOX_MCP_TOKEN_ID and
    PROXMOX_MCP_TOKEN_SECRET are both present; otherwise password authentication
    is used.

    Raises:
        EnvironmentError: if required variables are missing or the chosen
                          auth mode lacks its mandatory fields.
    """
    host = os.getenv("PROXMOX_MCP_HOST")
    port = os.getenv("PROXMOX_MCP_PORT", "8006")
    user = os.getenv("PROXMOX_MCP_USER")

    if not host:
        raise EnvironmentError("PROXMOX_MCP_HOST is not set.")
    if not user:
        raise EnvironmentError("PROXMOX_MCP_USER is not set.")

    # Parse PROXMOX_MCP_VERIFY_SSL — accepts "true"/"1"/"yes" (case-insensitive).
    # Defaults to False because most Proxmox installs use self-signed certs.
    verify_ssl_raw = os.getenv("PROXMOX_MCP_VERIFY_SSL", "false").strip().lower()
    verify_ssl = verify_ssl_raw in ("true", "1", "yes")

    token_id = os.getenv("PROXMOX_MCP_TOKEN_ID", "").strip()
    token_secret = os.getenv("PROXMOX_MCP_TOKEN_SECRET", "").strip()

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
    password = os.getenv("PROXMOX_MCP_PASSWORD", "").strip()
    if not password:
        raise EnvironmentError(
            "No valid credentials found.  Set either "
            "PROXMOX_MCP_TOKEN_ID + PROXMOX_MCP_TOKEN_SECRET (recommended) "
            "or PROXMOX_MCP_PASSWORD."
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

        # ── Disk management ───────────────────────────────────────────────

        types.Tool(
            name="vm_move_disk",
            description=(
                "Move a VM disk to a different storage pool. "
                "Optionally delete the source disk after the move completes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID (QEMU only)."},
                    "disk": {"type": "string", "description": "Disk to move, e.g. 'scsi0', 'virtio0'."},
                    "storage": {"type": "string", "description": "Destination storage pool."},
                    "delete": {"type": "boolean", "description": "Delete the source disk after move (default false)."},
                },
                "required": ["node", "vmid", "disk", "storage"],
            },
        ),
        types.Tool(
            name="vm_unlink_disk",
            description=(
                "Detach one or more disks from a QEMU VM configuration. "
                "With force=true the disk image is also deleted from storage. "
                "WARNING: force deletion is irreversible."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID (QEMU only)."},
                    "idlist": {"type": "string", "description": "Comma-separated disk names to unlink, e.g. 'scsi0,ide2'."},
                    "force": {"type": "boolean", "description": "Also delete the disk image from storage (default false)."},
                },
                "required": ["node", "vmid", "idlist"],
            },
        ),
        types.Tool(
            name="list_node_disks",
            description="List physical disks installed on a Proxmox node with model, size and S.M.A.R.T. health.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="vm_template",
            description=(
                "Convert a stopped QEMU VM into a template. "
                "Templates are read-only and can only be used as clone sources. "
                "WARNING: this conversion is irreversible."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID to convert (must be stopped)."},
                },
                "required": ["node", "vmid"],
            },
        ),

        # ── QEMU Guest Agent ──────────────────────────────────────────────

        types.Tool(
            name="vm_agent_exec",
            description=(
                "Execute a shell command inside a running QEMU VM via the QEMU guest agent. "
                "Requires qemu-guest-agent to be installed and running in the VM. "
                "Returns stdout, stderr and exit code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID."},
                    "command": {"type": "string", "description": "Shell command to run inside the VM, e.g. 'uptime'."},
                },
                "required": ["node", "vmid", "command"],
            },
        ),
        types.Tool(
            name="vm_agent_info",
            description=(
                "Retrieve OS and guest agent information from inside a running QEMU VM: "
                "OS name, kernel version, hostname, guest agent version."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID."},
                },
                "required": ["node", "vmid"],
            },
        ),
        types.Tool(
            name="vm_agent_network",
            description=(
                "Retrieve the actual network interface configuration from inside a QEMU VM "
                "via the guest agent — shows real IPs assigned by the OS, not just the "
                "Proxmox configuration."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID."},
                },
                "required": ["node", "vmid"],
            },
        ),

        # ── Backup jobs ───────────────────────────────────────────────────

        types.Tool(
            name="list_backup_jobs",
            description="List all scheduled vzdump backup jobs configured in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="prune_backups",
            description=(
                "Delete old backup archives from a storage pool according to a retention policy. "
                "Retention parameters follow the Proxmox keep-* conventions. "
                "Without retention params, performs a dry-run and shows what would be deleted."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "storage": {"type": "string", "description": "Storage pool to prune."},
                    "vmid": {"type": "integer", "description": "Restrict pruning to a specific VM/CT ID (optional)."},
                    "keep_last": {"type": "integer", "description": "Number of most recent backups to keep."},
                    "keep_daily": {"type": "integer", "description": "Number of daily backups to keep."},
                    "keep_weekly": {"type": "integer", "description": "Number of weekly backups to keep."},
                    "keep_monthly": {"type": "integer", "description": "Number of monthly backups to keep."},
                },
                "required": ["node", "storage"],
            },
        ),

        # ── Replication ───────────────────────────────────────────────────

        types.Tool(
            name="list_replication",
            description="List all ZFS replication jobs configured in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="create_replication",
            description=(
                "Create a ZFS replication job to continuously replicate a VM or container "
                "to another node.  The job ID format is '{vmid}-{jobnum}', e.g. '100-0'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Job ID in the format '{vmid}-{jobnum}', e.g. '100-0'."},
                    "target": {"type": "string", "description": "Destination node for replication."},
                    "schedule": {"type": "string", "description": "Replication schedule in systemd calendar format, e.g. '*/15' for every 15 minutes (default: '*/15')."},
                    "comment": {"type": "string", "description": "Optional description for this replication job."},
                },
                "required": ["id", "target"],
            },
        ),
        types.Tool(
            name="delete_replication",
            description="Delete a ZFS replication job.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Replication job ID (e.g. '100-0')."},
                    "force": {"type": "boolean", "description": "Force deletion even if the job is currently running."},
                },
                "required": ["id"],
            },
        ),

        # ── Ceph ──────────────────────────────────────────────────────────

        types.Tool(
            name="ceph_status",
            description="Full Ceph cluster status: health, IOPS, throughput, PG states and monitor quorum.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Any Proxmox node that runs Ceph."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="ceph_health",
            description="Ceph health checks — detailed list of warnings and errors affecting the cluster.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Any Proxmox node that runs Ceph."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="ceph_osds",
            description="List all Ceph OSD daemons with their status (up/down/in/out), weight and device path.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Any Proxmox node that runs Ceph."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="ceph_pools",
            description="List Ceph storage pools with size, replication factor and I/O statistics.",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Any Proxmox node that runs Ceph."}},
                "required": ["node"],
            },
        ),

        # ── Node — OS actions ─────────────────────────────────────────────

        types.Tool(
            name="node_reboot",
            description=(
                "Reboot a Proxmox node. All VMs and containers on the node will be "
                "stopped (or migrated if HA is configured) before the reboot."
            ),
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node to reboot."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_shutdown",
            description=(
                "Shut down a Proxmox node. All VMs and containers on the node will be "
                "stopped before shutdown."
            ),
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node to shut down."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_apt_upgrade",
            description=(
                "Refresh the APT package list on a node and return the list of upgradable packages. "
                "This is equivalent to running 'apt-get update'. "
                "To apply upgrades, use the Proxmox web UI or SSH into the node."
            ),
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),
        types.Tool(
            name="node_certificates",
            description="Retrieve TLS certificate information for a Proxmox node (subject, issuer, expiry).",
            inputSchema={
                "type": "object",
                "properties": {"node": {"type": "string", "description": "Proxmox node name."}},
                "required": ["node"],
            },
        ),

        # ── Notifications ─────────────────────────────────────────────────

        types.Tool(
            name="list_notification_endpoints",
            description="List configured notification endpoints (email, Gotify, webhook, SMTP) in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_notification_matchers",
            description="List notification matchers — rules that route cluster events to notification endpoints.",
            inputSchema={"type": "object", "properties": {}},
        ),

        # ── Console ───────────────────────────────────────────────────────

        types.Tool(
            name="vm_console_url",
            description=(
                "Generate a noVNC console URL for a running QEMU VM or LXC container. "
                "The returned URL can be opened in a browser to access the graphical console. "
                "The ticket is valid for a short time only."
            ),
            inputSchema={"type": "object", "properties": _NODE_VMID_TYPE, "required": _REQUIRED_NVT},
        ),

        # ── Task management ───────────────────────────────────────────────────

        types.Tool(
            name="wait_for_task",
            description=(
                "Poll a Proxmox task (UPID) until it finishes and return the final status. "
                "Useful after create_backup, vm_clone, vm_migrate and other async operations."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node that owns the task."},
                    "upid": {"type": "string", "description": "Task UPID string returned by the async operation."},
                    "timeout": {"type": "integer", "description": "Maximum seconds to wait (default 120)."},
                },
                "required": ["node", "upid"],
            },
        ),
        types.Tool(
            name="cancel_task",
            description="Cancel a running Proxmox task by its UPID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Node that owns the task."},
                    "upid": {"type": "string", "description": "Task UPID string to cancel."},
                },
                "required": ["node", "upid"],
            },
        ),

        # ── VM / CT configuration write ───────────────────────────────────────

        types.Tool(
            name="vm_set_config",
            description=(
                "Update one or more configuration parameters of a VM or container. "
                "Accepts any Proxmox config key in config_params "
                "(e.g. name, memory, cores, description, tags, onboot, protection)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_NODE_VMID_TYPE,
                    "config_params": {
                        "type": "object",
                        "description": (
                            "Key-value pairs of Proxmox config options to set. "
                            "Examples: {\"memory\": 2048, \"cores\": 4, \"description\": \"prod\", "
                            "\"tags\": \"prod;web\", \"onboot\": 1, \"protection\": 0}"
                        ),
                    },
                },
                "required": [*_REQUIRED_NVT, "config_params"],
            },
        ),
        types.Tool(
            name="vm_set_cdrom",
            description=(
                "Mount or unmount an ISO image on a QEMU VM's CD-ROM drive. "
                "Pass iso_volid to mount (e.g. 'local:iso/debian-12.iso'), "
                "omit or pass empty string to eject."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "VM ID (QEMU only)."},
                    "iso_volid": {
                        "type": "string",
                        "description": "ISO volume ID to mount, e.g. 'local:iso/debian-12.iso'. Empty to eject.",
                    },
                    "ide_slot": {
                        "type": "string",
                        "description": "CD-ROM device slot (default: 'ide2').",
                    },
                },
                "required": ["node", "vmid"],
            },
        ),

        # ── LXC exec ──────────────────────────────────────────────────────────

        types.Tool(
            name="lxc_exec",
            description=(
                "Execute a command inside a running LXC container via the Proxmox API. "
                "Does not require a guest agent. Returns stdout, stderr and exit code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "vmid": {"type": "integer", "description": "LXC container ID."},
                    "command": {"type": "string", "description": "Shell command to execute, e.g. 'df -h'."},
                },
                "required": ["node", "vmid", "command"],
            },
        ),

        # ── Storage management ────────────────────────────────────────────────

        types.Tool(
            name="storage_status",
            description="Detailed usage statistics for a storage pool on a node: total, used, available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "storage": {"type": "string", "description": "Storage pool name."},
                },
                "required": ["node", "storage"],
            },
        ),
        types.Tool(
            name="create_storage",
            description=(
                "Add a new storage backend to the cluster. "
                "Supported types: dir, nfs, cifs, lvm, lvmthin, zfspool, rbd, cephfs, pbs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "storage": {"type": "string", "description": "Unique storage ID."},
                    "type": {
                        "type": "string",
                        "enum": ["dir", "nfs", "cifs", "lvm", "lvmthin", "zfspool", "rbd", "cephfs", "pbs"],
                        "description": "Storage backend type.",
                    },
                    "path": {"type": "string", "description": "Local directory path (type=dir)."},
                    "server": {"type": "string", "description": "NFS/CIFS/PBS server hostname or IP."},
                    "export": {"type": "string", "description": "NFS export path."},
                    "share": {"type": "string", "description": "CIFS share name."},
                    "vg_name": {"type": "string", "description": "LVM volume group name."},
                    "pool": {"type": "string", "description": "ZFS pool or Ceph pool name."},
                    "content": {"type": "string", "description": "Comma-separated content types: images,rootdir,vztmpl,backup,iso,snippets."},
                    "nodes": {"type": "string", "description": "Comma-separated nodes that can use this storage (empty = all)."},
                    "shared": {"type": "integer", "enum": [0, 1], "description": "1 if shared across all nodes."},
                },
                "required": ["storage", "type"],
            },
        ),
        types.Tool(
            name="delete_storage",
            description="Remove a storage backend from the cluster configuration. Does not delete underlying data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "storage": {"type": "string", "description": "Storage ID to remove."},
                },
                "required": ["storage"],
            },
        ),

        # ── Resource pool management ──────────────────────────────────────────

        types.Tool(
            name="create_pool",
            description="Create a new resource pool for grouping VMs and storage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "poolid": {"type": "string", "description": "Unique pool identifier."},
                    "comment": {"type": "string", "description": "Optional description."},
                },
                "required": ["poolid"],
            },
        ),
        types.Tool(
            name="delete_pool",
            description="Delete a resource pool. The pool must be empty.",
            inputSchema={
                "type": "object",
                "properties": {
                    "poolid": {"type": "string", "description": "Pool ID to delete."},
                },
                "required": ["poolid"],
            },
        ),
        types.Tool(
            name="pool_add_member",
            description="Add VMs and/or storage pools to a resource pool.",
            inputSchema={
                "type": "object",
                "properties": {
                    "poolid": {"type": "string", "description": "Pool ID."},
                    "vms": {"type": "string", "description": "Comma-separated VM/CT IDs to add, e.g. '100,101'."},
                    "storage": {"type": "string", "description": "Comma-separated storage IDs to add."},
                },
                "required": ["poolid"],
            },
        ),
        types.Tool(
            name="pool_remove_member",
            description="Remove VMs and/or storage pools from a resource pool.",
            inputSchema={
                "type": "object",
                "properties": {
                    "poolid": {"type": "string", "description": "Pool ID."},
                    "vms": {"type": "string", "description": "Comma-separated VM/CT IDs to remove."},
                    "storage": {"type": "string", "description": "Comma-separated storage IDs to remove."},
                },
                "required": ["poolid"],
            },
        ),

        # ── Network management ────────────────────────────────────────────────

        types.Tool(
            name="create_network",
            description=(
                "Create a network interface or bridge on a node. "
                "Changes are staged — call apply_network_config to activate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "iface": {"type": "string", "description": "Interface name, e.g. 'vmbr1'."},
                    "type": {
                        "type": "string",
                        "enum": ["bridge", "bond", "eth", "vlan", "alias", "OVSBridge", "OVSBond", "OVSPort", "OVSIntPort"],
                        "description": "Interface type.",
                    },
                    "cidr": {"type": "string", "description": "IPv4 address/prefix, e.g. '192.168.10.1/24'."},
                    "cidr6": {"type": "string", "description": "IPv6 address/prefix."},
                    "gateway": {"type": "string", "description": "IPv4 default gateway."},
                    "gateway6": {"type": "string", "description": "IPv6 default gateway."},
                    "bridge_ports": {"type": "string", "description": "Space-separated bridge ports, e.g. 'eth0'."},
                    "bond_slaves": {"type": "string", "description": "Space-separated bond slave interfaces."},
                    "vlan_id": {"type": "integer", "description": "VLAN tag ID."},
                    "vlan_raw_device": {"type": "string", "description": "Raw device for VLAN, e.g. 'eth0'."},
                    "autostart": {"type": "integer", "enum": [0, 1], "description": "Bring up at boot (default 1)."},
                    "comments": {"type": "string", "description": "Free-text comment."},
                },
                "required": ["node", "iface", "type"],
            },
        ),
        types.Tool(
            name="delete_network",
            description="Remove a staged network interface from a node. Call apply_network_config to activate.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "iface": {"type": "string", "description": "Interface name to remove."},
                },
                "required": ["node", "iface"],
            },
        ),
        types.Tool(
            name="apply_network_config",
            description=(
                "Apply pending network configuration changes on a node (ifreload -a). "
                "Required after create_network or delete_network."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                },
                "required": ["node"],
            },
        ),

        # ── Diagnostics ───────────────────────────────────────────────────────

        types.Tool(
            name="node_smart",
            description=(
                "Retrieve S.M.A.R.T. health data for a physical disk on a node. "
                "Use list_node_disks to find the device path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "disk": {"type": "string", "description": "Block device path, e.g. '/dev/sda'."},
                },
                "required": ["node", "disk"],
            },
        ),
        types.Tool(
            name="cluster_health_summary",
            description=(
                "Aggregate health check across the entire cluster: nodes, HA, tasks, "
                "Ceph (if configured) and storage. Single call for a fast overall assessment."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="node_top",
            description="Current resource snapshot for a node: CPU, memory, swap, disk I/O wait and VM/CT count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                },
                "required": ["node"],
            },
        ),

        # ── ACME / TLS ────────────────────────────────────────────────────────

        types.Tool(
            name="list_acme_accounts",
            description="List ACME (Let's Encrypt) accounts registered in the cluster.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="renew_certificate",
            description=(
                "Force renewal of the ACME/Let's Encrypt TLS certificate for a node. "
                "Requires ACME to be configured. The API may be briefly unavailable during renewal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "node": {"type": "string", "description": "Proxmox node name."},
                    "force": {"type": "boolean", "description": "Renew even if not yet expiring (default false)."},
                },
                "required": ["node"],
            },
        ),

        # ── API token management ──────────────────────────────────────────────

        types.Tool(
            name="create_api_token",
            description=(
                "Create a new API token for a Proxmox user. "
                "The token secret is returned only once — store it immediately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "userid": {"type": "string", "description": "Full user ID, e.g. 'root@pam'."},
                    "tokenid": {"type": "string", "description": "Token name (alphanumeric)."},
                    "comment": {"type": "string", "description": "Optional description."},
                    "expire": {"type": "integer", "description": "Expiry as Unix timestamp (0 = never, default 0)."},
                    "privsep": {"type": "integer", "enum": [0, 1], "description": "1 = separate token privileges from user (default 1)."},
                },
                "required": ["userid", "tokenid"],
            },
        ),
        types.Tool(
            name="delete_api_token",
            description="Revoke and delete an API token.",
            inputSchema={
                "type": "object",
                "properties": {
                    "userid": {"type": "string", "description": "Full user ID, e.g. 'root@pam'."},
                    "tokenid": {"type": "string", "description": "Token name to delete."},
                },
                "required": ["userid", "tokenid"],
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

    # ── Disk management ───────────────────────────────────────────────────

    if name == "vm_move_disk":
        node, vmid = arguments["node"], arguments["vmid"]
        disk, storage = arguments["disk"], arguments["storage"]
        # POST /nodes/{node}/qemu/{vmid}/move_disk
        # Proxmox copies the disk image to the target storage and updates the VM config.
        params = {"disk": disk, "storage": storage}
        if arguments.get("delete"):
            params["delete"] = 1  # remove source image after successful copy
        result = proxmox.nodes(node).qemu(vmid).move_disk.post(**params)
        return [types.TextContent(type="text", text=f"Move disk task queued: {result}")]

    if name == "vm_unlink_disk":
        node, vmid = arguments["node"], arguments["vmid"]
        idlist = arguments["idlist"]
        # PUT /nodes/{node}/qemu/{vmid}/unlink
        # idlist is a comma-separated string of disk identifiers (e.g. "scsi0,ide2").
        params = {"idlist": idlist}
        if arguments.get("force"):
            params["force"] = 1  # also delete the underlying image from storage
        proxmox.nodes(node).qemu(vmid).unlink.put(**params)
        return [types.TextContent(type="text", text=f"Disk(s) {idlist} unlinked from VM {vmid}.")]

    if name == "list_node_disks":
        node = arguments["node"]
        # GET /nodes/{node}/disks/list — physical block devices on the host.
        disks = proxmox.nodes(node).disks.list.get()
        lines = [
            f"{d.get('devpath', '?')} | {d.get('model', 'N/A')} | "
            f"size={util.bytes_to_human_readable(d.get('size', 0))} | "
            f"health={d.get('health', 'N/A')} | type={d.get('type', '?')}"
            for d in disks
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No disks found.")]

    if name == "vm_template":
        node, vmid = arguments["node"], arguments["vmid"]
        # POST /nodes/{node}/qemu/{vmid}/template
        # Marks the VM as a template — sets read-only flag, removes volatile config (e.g. MAC).
        # The VM must be stopped before conversion.
        proxmox.nodes(node).qemu(vmid).template.post()
        return [types.TextContent(type="text", text=f"VM {vmid} converted to template.")]

    # ── QEMU Guest Agent ───────────────────────────────────────────────────

    if name == "vm_agent_exec":
        import time
        node, vmid = arguments["node"], arguments["vmid"]
        command = arguments["command"]
        # POST /nodes/{node}/qemu/{vmid}/agent/exec
        # The guest agent runs the command asynchronously and returns a PID.
        # We poll /agent/exec-status until the process exits (up to 10 seconds).
        try:
            result = proxmox.nodes(node).qemu(vmid).agent.exec.post(
                command=command,
                **{"input-data": ""}  # required by the API even if empty
            )
            pid = result["pid"]
            for _ in range(20):
                time.sleep(0.5)
                status = proxmox.nodes(node).qemu(vmid).agent("exec-status").get(pid=pid)
                if status.get("exited"):
                    lines = [
                        f"exit_code={status.get('exitcode', '?')}",
                        f"stdout:\n{status.get('out-data', '').strip()}",
                    ]
                    if status.get("err-data", "").strip():
                        lines.append(f"stderr:\n{status['err-data'].strip()}")
                    return [types.TextContent(type="text", text="\n".join(lines))]
            return [types.TextContent(type="text", text=f"Command still running (pid={pid}). Check node_tasks for the result.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Guest agent error: {e}. Ensure qemu-guest-agent is installed and running.")]

    if name == "vm_agent_info":
        node, vmid = arguments["node"], arguments["vmid"]
        # GET /nodes/{node}/qemu/{vmid}/agent/get-osinfo — OS information from inside the VM.
        try:
            info = proxmox.nodes(node).qemu(vmid).agent("get-osinfo").get()
            result = info.get("result", info)
            lines = [f"{k}: {v}" for k, v in sorted(result.items()) if v is not None]
            return [types.TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Guest agent error: {e}. Ensure qemu-guest-agent is installed and running.")]

    if name == "vm_agent_network":
        node, vmid = arguments["node"], arguments["vmid"]
        # GET /nodes/{node}/qemu/{vmid}/agent/network-get-interfaces
        # Returns interfaces as seen inside the guest OS — actual IPs, not Proxmox config.
        try:
            data = proxmox.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            interfaces = data.get("result", [])
            lines = []
            for iface in interfaces:
                addrs = iface.get("ip-addresses", [])
                addr_str = ", ".join(
                    f"{a['ip-address']}/{a.get('prefix', '?')}"
                    for a in addrs
                    if a.get("ip-address-type") in ("ipv4", "ipv6")
                )
                lines.append(
                    f"{iface.get('name', '?')} | "
                    f"mac={iface.get('hardware-address', 'N/A')} | "
                    f"ips=[{addr_str}]"
                )
            return [types.TextContent(type="text", text="\n".join(lines) or "No interfaces found.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Guest agent error: {e}. Ensure qemu-guest-agent is installed and running.")]

    # ── Backup jobs ────────────────────────────────────────────────────────

    if name == "list_backup_jobs":
        # GET /cluster/backup — scheduled vzdump job definitions.
        # Each job has: id, enabled, schedule, storage, vmid list, mode, compress, etc.
        jobs = proxmox.cluster.backup.get()
        lines = [
            f"{j.get('id', '?')} | enabled={j.get('enabled', 1)} | "
            f"schedule={j.get('schedule', '?')} | storage={j.get('storage', '?')} | "
            f"vmids={j.get('vmid', 'all')} | mode={j.get('mode', '?')} | "
            f"compress={j.get('compress', '?')}"
            for j in jobs
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No backup jobs configured.")]

    if name == "prune_backups":
        node, storage = arguments["node"], arguments["storage"]
        # DELETE /nodes/{node}/storage/{storage}/prunebackups
        # Without keep-* parameters Proxmox performs a dry-run and returns what would be removed.
        params = {}
        if "vmid" in arguments:
            params["vmid"] = arguments["vmid"]
        for key in ("keep_last", "keep_daily", "keep_weekly", "keep_monthly"):
            if key in arguments:
                # Proxmox param names use hyphens: keep-last, keep-daily, etc.
                params[key.replace("_", "-")] = arguments[key]
        result = proxmox.nodes(node).storage(storage).prunebackups.delete(**params)
        if isinstance(result, list):
            lines = [
                f"{r.get('volid', '?')} | {r.get('type', '?')} | mark={r.get('mark', '?')}"
                for r in result
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "Nothing to prune.")]
        return [types.TextContent(type="text", text=f"Prune task queued: {result}")]

    # ── Replication ────────────────────────────────────────────────────────

    if name == "list_replication":
        # GET /cluster/replication — all ZFS replication jobs.
        jobs = proxmox.cluster.replication.get()
        lines = [
            f"{j.get('id', '?')} | target={j.get('target', '?')} | "
            f"schedule={j.get('schedule', '?')} | disabled={j.get('disable', 0)} | "
            f"comment={j.get('comment', '')}"
            for j in jobs
        ]
        return [types.TextContent(type="text", text="\n".join(lines) or "No replication jobs configured.")]

    if name == "create_replication":
        # POST /cluster/replication
        # type is always "local" (only local ZFS replication is supported).
        params = {
            "id": arguments["id"],
            "target": arguments["target"],
            "type": "local",
            "schedule": arguments.get("schedule", "*/15"),  # default: every 15 minutes
        }
        if "comment" in arguments:
            params["comment"] = arguments["comment"]
        proxmox.cluster.replication.post(**params)
        return [types.TextContent(type="text", text=f"Replication job {arguments['id']} created.")]

    if name == "delete_replication":
        rep_id = arguments["id"]
        # DELETE /cluster/replication/{id}
        params = {}
        if arguments.get("force"):
            params["force"] = 1
        proxmox.cluster.replication(rep_id).delete(**params)
        return [types.TextContent(type="text", text=f"Replication job {rep_id} deleted.")]

    # ── Ceph ───────────────────────────────────────────────────────────────

    if name == "ceph_status":
        node = arguments["node"]
        try:
            # GET /nodes/{node}/ceph/status — full Ceph cluster overview.
            status = proxmox.nodes(node).ceph.status.get()
            health = status.get("health", {})
            pgmap = status.get("pgmap", {})
            lines = [
                f"health: {health.get('status', 'N/A')}",
                f"pgs: {pgmap.get('num_pgs', '?')} | "
                f"read: {util.bytes_to_human_readable(pgmap.get('read_bytes_sec', 0))}/s | "
                f"write: {util.bytes_to_human_readable(pgmap.get('write_bytes_sec', 0))}/s",
                f"osds: total={status.get('osdmap', {}).get('num_osds', '?')} "
                f"up={status.get('osdmap', {}).get('num_up_osds', '?')} "
                f"in={status.get('osdmap', {}).get('num_in_osds', '?')}",
            ]
            for check in health.get("checks", {}).values():
                lines.append(f"  [{check.get('severity', '?')}] {check.get('summary', {}).get('message', '')}")
            return [types.TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Ceph not available: {e}")]

    if name == "ceph_health":
        node = arguments["node"]
        try:
            status = proxmox.nodes(node).ceph.status.get()
            health = status.get("health", {})
            overall = health.get("status", "N/A")
            checks = health.get("checks", {})
            lines = [f"Overall: {overall}"]
            for check_name, check in checks.items():
                lines.append(
                    f"  {check_name} [{check.get('severity', '?')}]: "
                    f"{check.get('summary', {}).get('message', '')}"
                )
            return [types.TextContent(type="text", text="\n".join(lines) or f"Health: {overall} — no active checks.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Ceph not available: {e}")]

    if name == "ceph_osds":
        node = arguments["node"]
        try:
            # GET /nodes/{node}/ceph/osd — OSD tree with status fields.
            data = proxmox.nodes(node).ceph.osd.get()
            osds = data.get("tree", [])
            lines = [
                f"osd.{o.get('id', '?')} | up={o.get('up', '?')} | in={o.get('in', '?')} | "
                f"weight={o.get('crush_weight', '?')} | device={o.get('device_class', '?')}"
                for o in osds if o.get("type") == "osd"
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No OSDs found.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Ceph not available: {e}")]

    if name == "ceph_pools":
        node = arguments["node"]
        try:
            # GET /nodes/{node}/ceph/pools — Ceph storage pool list with stats.
            pools = proxmox.nodes(node).ceph.pools.get()
            lines = [
                f"{p.get('pool_name', '?')} | size={p.get('size', '?')} | "
                f"used={util.bytes_to_human_readable(p.get('bytes_used', 0))} | "
                f"avail={util.bytes_to_human_readable(p.get('avail_raw', 0))} | "
                f"read={util.bytes_to_human_readable(p.get('read_bytes_sec', 0))}/s | "
                f"write={util.bytes_to_human_readable(p.get('write_bytes_sec', 0))}/s"
                for p in pools
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No Ceph pools found.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Ceph not available: {e}")]

    # ── Node — OS actions ──────────────────────────────────────────────────

    if name == "node_reboot":
        node = arguments["node"]
        # POST /nodes/{node}/status — command=reboot.
        # Proxmox will attempt to gracefully stop VMs and containers first.
        proxmox.nodes(node).status.post(command="reboot")
        return [types.TextContent(type="text", text=f"Node {node} is rebooting.")]

    if name == "node_shutdown":
        node = arguments["node"]
        # POST /nodes/{node}/status — command=shutdown.
        proxmox.nodes(node).status.post(command="shutdown")
        return [types.TextContent(type="text", text=f"Node {node} is shutting down.")]

    if name == "node_apt_upgrade":
        node = arguments["node"]
        # GET /nodes/{node}/apt/update?force=1 — forces an apt-get update (package list refresh)
        # and returns the list of upgradable packages.  Actual upgrade requires apt-get upgrade
        # which must be run manually via the Proxmox shell or the web UI upgrade wizard.
        updates = proxmox.nodes(node).apt.update.get(force=1)
        lines = [
            f"{u.get('Package', '?')} | {u.get('OldVersion', '?')} → {u.get('Version', '?')} | "
            f"priority={u.get('Priority', '?')}"
            for u in updates
        ]
        header = f"Package list refreshed. {len(lines)} update(s) available:\n" if lines else "Package list refreshed. System is up to date."
        return [types.TextContent(type="text", text=header + "\n".join(lines))]

    if name == "node_certificates":
        node = arguments["node"]
        # GET /nodes/{node}/certificates/info — TLS certificate details for the node's API.
        certs = proxmox.nodes(node).certificates.info.get()
        lines = []
        for cert in certs:
            lines += [
                f"filename: {cert.get('filename', '?')}",
                f"subject:  {cert.get('subject', 'N/A')}",
                f"issuer:   {cert.get('issuer', 'N/A')}",
                f"valid:    {cert.get('notbefore', 'N/A')} → {cert.get('notafter', 'N/A')}",
                f"san:      {', '.join(cert.get('san', []))}",
                "---",
            ]
        return [types.TextContent(type="text", text="\n".join(lines).rstrip("---").strip())]

    # ── Notifications ──────────────────────────────────────────────────────

    if name == "list_notification_endpoints":
        # GET /cluster/notifications/endpoints — Proxmox 8.1+ notification system.
        try:
            endpoints = proxmox.cluster.notifications.endpoints.get()
            lines = [
                f"{e.get('name', '?')} | type={e.get('type', '?')} | "
                f"enabled={e.get('enable', 1)} | comment={e.get('comment', '')}"
                for e in endpoints
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No notification endpoints configured.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Notifications API not available (requires Proxmox 8.1+): {e}")]

    if name == "list_notification_matchers":
        # GET /cluster/notifications/matchers — routing rules for cluster events.
        try:
            matchers = proxmox.cluster.notifications.matchers.get()
            lines = [
                f"{m.get('name', '?')} | enabled={m.get('enable', 1)} | "
                f"targets={m.get('target', [])} | comment={m.get('comment', '')}"
                for m in matchers
            ]
            return [types.TextContent(type="text", text="\n".join(lines) or "No notification matchers configured.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Notifications API not available (requires Proxmox 8.1+): {e}")]

    # ── Console ────────────────────────────────────────────────────────────

    if name == "vm_console_url":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        host = os.getenv("PROXMOX_MCP_HOST", "localhost")
        port_str = os.getenv("PROXMOX_MCP_PORT", "8006")
        try:
            # POST /nodes/{node}/{type}/{vmid}/vncproxy — obtain a VNC ticket and port.
            # websocket=1 requests a WebSocket-compatible ticket for noVNC.
            ticket_data = _vm_api(node, vmid, vm_type).vncproxy.post(websocket=1)
            ticket = ticket_data.get("ticket", "")
            vnc_port = ticket_data.get("port", "")
            # Construct the noVNC URL using the Proxmox built-in web console.
            url = (
                f"https://{host}:{port_str}/?console={'kvm' if vm_type == 'qemu' else 'lxc'}"
                f"&novnc=1&vmid={vmid}&node={node}&resize=scale"
                f"&port={vnc_port}&ticket={ticket}"
            )
            return [types.TextContent(type="text", text=f"Console URL (valid for ~30s):\n{url}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Could not generate console URL: {e}")]

    # ── Task management ────────────────────────────────────────────────────────

    if name == "wait_for_task":
        import time
        node = arguments["node"]
        upid = arguments["upid"]
        timeout = arguments.get("timeout", 120)
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = proxmox.nodes(node).tasks(upid).status.get()
            if status.get("status") != "running":
                return [types.TextContent(type="text", text=(
                    f"Task finished.\n"
                    f"status:    {status.get('status', '?')}\n"
                    f"exitstatus: {status.get('exitstatus', 'N/A')}\n"
                    f"upid:      {upid}"
                ))]
            time.sleep(2)
        return [types.TextContent(type="text", text=f"Timeout after {timeout}s — task still running: {upid}")]

    if name == "cancel_task":
        node = arguments["node"]
        upid = arguments["upid"]
        # DELETE /nodes/{node}/tasks/{upid} — stops a running task.
        proxmox.nodes(node).tasks(upid).delete()
        return [types.TextContent(type="text", text=f"Task {upid} cancellation requested.")]

    # ── VM / CT configuration write ────────────────────────────────────────────

    if name == "vm_set_config":
        node, vmid, vm_type = arguments["node"], arguments["vmid"], arguments["type"]
        params = arguments["config_params"]
        # PUT /nodes/{node}/{type}/{vmid}/config — partial update; only sent keys are changed.
        _vm_api(node, vmid, vm_type).config.put(**params)
        return [types.TextContent(type="text", text=f"Config updated for {vm_type} {vmid}: {list(params.keys())}")]

    if name == "vm_set_cdrom":
        node, vmid = arguments["node"], arguments["vmid"]
        slot = arguments.get("ide_slot", "ide2")
        iso = arguments.get("iso_volid", "").strip()
        # Build the device string: either "volid,media=cdrom" or "none,media=cdrom" to eject.
        device_str = f"{iso},media=cdrom" if iso else "none,media=cdrom"
        proxmox.nodes(node).qemu(vmid).config.put(**{slot: device_str})
        action = f"mounted '{iso}'" if iso else "ejected"
        return [types.TextContent(type="text", text=f"CD-ROM {slot} {action} on VM {vmid}.")]

    # ── LXC exec ───────────────────────────────────────────────────────────────

    if name == "lxc_exec":
        import time, shlex
        node, vmid = arguments["node"], arguments["vmid"]
        command = arguments["command"]
        # POST /nodes/{node}/lxc/{vmid}/exec — async, returns PID; poll exec-status.
        try:
            cmd_parts = shlex.split(command)
            result = proxmox.nodes(node).lxc(vmid).exec.post(
                command=cmd_parts
            )
            pid = result["pid"]
            for _ in range(30):
                time.sleep(0.5)
                st = proxmox.nodes(node).lxc(vmid)("exec-status").get(pid=pid)
                if st.get("exited"):
                    lines = [f"exit_code={st.get('exitcode', '?')}",
                             f"stdout:\n{st.get('out-data', '').strip()}"]
                    if st.get("err-data", "").strip():
                        lines.append(f"stderr:\n{st['err-data'].strip()}")
                    return [types.TextContent(type="text", text="\n".join(lines))]
            return [types.TextContent(type="text", text=f"Command still running (pid={pid}).")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"LXC exec error: {e}")]

    # ── Storage management ──────────────────────────────────────────────────────

    if name == "storage_status":
        node, storage = arguments["node"], arguments["storage"]
        # GET /nodes/{node}/storage/{storage}/status — live usage counters.
        st = proxmox.nodes(node).storage(storage).status.get()
        lines = [
            f"storage:  {st.get('storage', storage)}",
            f"type:     {st.get('type', '?')}",
            f"total:    {util.bytes_to_human_readable(st.get('total', 0))}",
            f"used:     {util.bytes_to_human_readable(st.get('used', 0))} "
            f"({util.decimaltopercentage(st.get('used', 0) / st.get('total', 1) if st.get('total') else 0)})",
            f"avail:    {util.bytes_to_human_readable(st.get('avail', 0))}",
            f"active:   {st.get('active', '?')}",
            f"enabled:  {st.get('enabled', '?')}",
        ]
        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "create_storage":
        params = {k: v for k, v in arguments.items()}
        # POST /storage — cluster-level storage definition.
        proxmox.storage.post(**params)
        return [types.TextContent(type="text", text=f"Storage '{arguments['storage']}' ({arguments['type']}) created.")]

    if name == "delete_storage":
        storage = arguments["storage"]
        # DELETE /storage/{storage} — removes the config entry only.
        proxmox.storage(storage).delete()
        return [types.TextContent(type="text", text=f"Storage '{storage}' removed from configuration.")]

    # ── Resource pool management ────────────────────────────────────────────────

    if name == "create_pool":
        params = {"poolid": arguments["poolid"]}
        if "comment" in arguments:
            params["comment"] = arguments["comment"]
        proxmox.pools.post(**params)
        return [types.TextContent(type="text", text=f"Pool '{arguments['poolid']}' created.")]

    if name == "delete_pool":
        proxmox.pools(arguments["poolid"]).delete()
        return [types.TextContent(type="text", text=f"Pool '{arguments['poolid']}' deleted.")]

    if name == "pool_add_member":
        poolid = arguments["poolid"]
        params = {}
        if "vms" in arguments:
            params["vms"] = arguments["vms"]
        if "storage" in arguments:
            params["storage"] = arguments["storage"]
        proxmox.pools(poolid).put(**params)
        return [types.TextContent(type="text", text=f"Members added to pool '{poolid}'.")]

    if name == "pool_remove_member":
        poolid = arguments["poolid"]
        params = {"delete": 1}
        if "vms" in arguments:
            params["vms"] = arguments["vms"]
        if "storage" in arguments:
            params["storage"] = arguments["storage"]
        proxmox.pools(poolid).put(**params)
        return [types.TextContent(type="text", text=f"Members removed from pool '{poolid}'.")]

    # ── Network management ──────────────────────────────────────────────────────

    if name == "create_network":
        node = arguments["node"]
        params = {k: v for k, v in arguments.items() if k != "node"}
        proxmox.nodes(node).network.post(**params)
        return [types.TextContent(type="text", text=f"Network '{arguments['iface']}' staged on {node}. Run apply_network_config to activate.")]

    if name == "delete_network":
        node, iface = arguments["node"], arguments["iface"]
        # DELETE /nodes/{node}/network/{iface} — removes from staged config.
        proxmox.nodes(node).network(iface).delete()
        return [types.TextContent(type="text", text=f"Network '{iface}' staged for removal on {node}. Run apply_network_config to activate.")]

    if name == "apply_network_config":
        node = arguments["node"]
        # PUT /nodes/{node}/network — reloads interfaces (ifreload -a).
        proxmox.nodes(node).network.put()
        return [types.TextContent(type="text", text=f"Network configuration applied on {node}.")]

    # ── Diagnostics ─────────────────────────────────────────────────────────────

    if name == "node_smart":
        node, disk = arguments["node"], arguments["disk"]
        # GET /nodes/{node}/disks/smart?disk={path}
        try:
            data = proxmox.nodes(node).disks.smart.get(disk=disk)
            health = data.get("health", "N/A")
            attrs = data.get("attributes", []) or data.get("data", [])
            lines = [f"disk:   {disk}", f"health: {health}"]
            for a in attrs[:20]:    # cap at 20 attributes for readability
                lines.append(
                    f"  {a.get('name', a.get('id', '?'))}: "
                    f"raw={a.get('raw', a.get('value', '?'))} "
                    f"thresh={a.get('threshold', 'N/A')}"
                )
            return [types.TextContent(type="text", text="\n".join(lines))]
        except Exception as e:
            return [types.TextContent(type="text", text=f"S.M.A.R.T. error: {e}")]

    if name == "cluster_health_summary":
        lines = ["=== CLUSTER HEALTH SUMMARY ===\n"]

        # Nodes
        try:
            nodes = proxmox.nodes.get()
            lines.append("── Nodes ──")
            for n in nodes:
                status = n.get("status", "?")
                flag = "✅" if status == "online" else "⚠️ "
                lines.append(
                    f"  {flag} {n['node']} | {status} | "
                    f"CPU: {util.decimaltopercentage(n.get('cpu', 0))} | "
                    f"Mem: {util.bytes_to_human_readable(n.get('mem', 0))}/{util.bytes_to_human_readable(n.get('maxmem', 0))}"
                )
        except Exception as e:
            lines.append(f"  Nodes error: {e}")

        # HA
        try:
            ha_items = proxmox.cluster.status.get()
            quorate = next((i.get("quorate") for i in ha_items if i.get("type") == "cluster"), None)
            flag = "✅" if quorate else "⚠️ "
            lines.append(f"\n── Cluster ── {flag} quorate={quorate}")
        except Exception as e:
            lines.append(f"\n  HA/cluster error: {e}")

        # Running tasks
        try:
            tasks = proxmox.cluster.tasks.get()
            running = [t for t in tasks if t.get("status") == "running"]
            flag = "⚠️ " if running else "✅"
            lines.append(f"\n── Tasks ── {flag} {len(running)} running")
            for t in running[:5]:
                lines.append(f"  {t.get('node', '?')} | {t.get('type', '?')} | vmid={t.get('id', '?')}")
        except Exception as e:
            lines.append(f"\n  Tasks error: {e}")

        # Ceph (optional)
        try:
            first_node = proxmox.nodes.get()[0]["node"]
            ceph = proxmox.nodes(first_node).ceph.status.get()
            health = ceph.get("health", {}).get("status", "N/A")
            flag = "✅" if health == "HEALTH_OK" else "⚠️ "
            lines.append(f"\n── Ceph ── {flag} {health}")
        except Exception:
            lines.append("\n── Ceph ── not configured or not accessible")

        # Storage
        try:
            storages = proxmox.storage.get()
            lines.append(f"\n── Storage ── {len(storages)} pool(s)")
            for s in storages:
                lines.append(f"  {s.get('storage', '?')} | type={s.get('type', '?')} | content={s.get('content', '?')}")
        except Exception as e:
            lines.append(f"\n  Storage error: {e}")

        return [types.TextContent(type="text", text="\n".join(lines))]

    if name == "node_top":
        node = arguments["node"]
        # GET /nodes/{node}/status — comprehensive node metrics snapshot.
        st = proxmox.nodes(node).status.get()
        cpu = st.get("cpu", 0)
        mem = st.get("memory", {})
        swap = st.get("swap", {})
        disk = st.get("rootfs", {})
        load = st.get("loadavg", [0, 0, 0])
        ksm = st.get("ksm", {})
        # Count running VMs and containers
        try:
            qemu_list = proxmox.nodes(node).qemu.get()
            lxc_list = proxmox.nodes(node).lxc.get()
            running_qemu = sum(1 for v in qemu_list if v.get("status") == "running")
            running_lxc = sum(1 for v in lxc_list if v.get("status") == "running")
        except Exception:
            running_qemu = running_lxc = 0
        lines = [
            f"node:     {node}",
            f"cpu:      {util.decimaltopercentage(cpu)}",
            f"load:     {load[0]} / {load[1]} / {load[2]} (1m/5m/15m)",
            f"memory:   {util.bytes_to_human_readable(mem.get('used', 0))}/{util.bytes_to_human_readable(mem.get('total', 0))} "
            f"({util.decimaltopercentage(mem.get('used', 0) / mem.get('total', 1) if mem.get('total') else 0)})",
            f"swap:     {util.bytes_to_human_readable(swap.get('used', 0))}/{util.bytes_to_human_readable(swap.get('total', 0))}",
            f"rootfs:   {util.bytes_to_human_readable(disk.get('used', 0))}/{util.bytes_to_human_readable(disk.get('total', 0))}",
            f"uptime:   {util.second_to_human_readable(st.get('uptime', 0))}",
            f"vms:      {running_qemu} QEMU running / {len(qemu_list)} total",
            f"cts:      {running_lxc} LXC running / {len(lxc_list)} total",
        ]
        if ksm:
            lines.append(f"ksm:      shared={util.bytes_to_human_readable(ksm.get('shared', 0))}")
        return [types.TextContent(type="text", text="\n".join(lines))]

    # ── ACME / TLS ──────────────────────────────────────────────────────────────

    if name == "list_acme_accounts":
        try:
            accounts = proxmox.cluster.acme.account.get()
            lines = [f"{a.get('name', '?')} | {a.get('url', 'N/A')}" for a in accounts]
            return [types.TextContent(type="text", text="\n".join(lines) or "No ACME accounts configured.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"ACME error: {e}")]

    if name == "renew_certificate":
        node = arguments["node"]
        force = 1 if arguments.get("force") else 0
        try:
            # POST /nodes/{node}/certificates/acme/certificate — triggers renewal.
            # force=1 renews even if the certificate is not yet expiring.
            result = proxmox.nodes(node).certificates.acme.certificate.post(force=force)
            return [types.TextContent(type="text", text=f"Certificate renewal started: {result}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Certificate renewal error: {e}")]

    # ── API token management ────────────────────────────────────────────────────

    if name == "create_api_token":
        userid = arguments["userid"]
        tokenid = arguments["tokenid"]
        params = {
            "expire": arguments.get("expire", 0),
            "privsep": arguments.get("privsep", 1),
        }
        if "comment" in arguments:
            params["comment"] = arguments["comment"]
        # POST /access/users/{userid}/token/{tokenid} — returns value (shown once only).
        result = proxmox.access.users(userid).token(tokenid).post(**params)
        secret = result.get("value", "N/A")
        return [types.TextContent(type="text", text=(
            f"Token created.\n"
            f"full_tokenid: {userid}!{tokenid}\n"
            f"secret:       {secret}\n"
            "⚠️  The secret is shown only once. Store it immediately."
        ))]

    if name == "delete_api_token":
        userid = arguments["userid"]
        tokenid = arguments["tokenid"]
        proxmox.access.users(userid).token(tokenid).delete()
        return [types.TextContent(type="text", text=f"Token '{userid}!{tokenid}' deleted.")]

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
