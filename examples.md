# Proxmox MCP — Tool Call Examples

Two usage patterns are shown for each tool:

- **Agent prompt** — natural language question to type in `agent.py`
- **Direct JSON** — raw MCP `tools/call` payload for testing with `server.py` directly

To test a direct JSON call:
```bash
printf '
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"<TOOL>","arguments":{...}}}
' | .venv/bin/python server.py
```

---

## Informational — read-only

### list_nodes
```
Agent  : "Show me all nodes in the cluster"
```
```json
{"name": "list_nodes", "arguments": {}}
```

### list_vms
```
Agent  : "List all VMs and containers on node pve"
```
```json
{"name": "list_vms", "arguments": {"node": "pve"}}
```

### list_storage
```
Agent  : "What storage pools are configured in the cluster?"
```
```json
{"name": "list_storage", "arguments": {}}
```

### vm_status
```
Agent  : "What is the current status of VM 100 on node pve?"
```
```json
{"name": "vm_status", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```
```json
{"name": "vm_status", "arguments": {"node": "pve", "vmid": 200, "type": "lxc"}}
```

### vm_config
```
Agent  : "Show the full configuration of VM 100 on node pve"
```
```json
{"name": "vm_config", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```

### cluster_resources
```
Agent  : "Give me a unified overview of all cluster resources"
Agent  : "Show only the VMs in the cluster"
Agent  : "List all storage resources"
```
```json
{"name": "cluster_resources", "arguments": {}}
{"name": "cluster_resources", "arguments": {"type": "vm"}}
{"name": "cluster_resources", "arguments": {"type": "storage"}}
{"name": "cluster_resources", "arguments": {"type": "node"}}
```

### cluster_tasks
```
Agent  : "What tasks are running or recently completed in the cluster?"
```
```json
{"name": "cluster_tasks", "arguments": {}}
```

### node_tasks
```
Agent  : "Show me the last 20 tasks on node pve"
```
```json
{"name": "node_tasks", "arguments": {"node": "pve"}}
{"name": "node_tasks", "arguments": {"node": "pve", "limit": 20}}
```

### list_snapshots
```
Agent  : "List all snapshots of VM 100 on node pve"
```
```json
{"name": "list_snapshots", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```

### storage_content
```
Agent  : "What files are stored in the local storage on node pve?"
Agent  : "List all ISO images available on node pve"
```
```json
{"name": "storage_content", "arguments": {"node": "pve", "storage": "local"}}
{"name": "storage_content", "arguments": {"node": "pve", "storage": "local-lvm"}}
```

### node_network
```
Agent  : "Show the network configuration of node pve"
```
```json
{"name": "node_network", "arguments": {"node": "pve"}}
```

---

## Backup

### list_backups
```
Agent  : "List all backup archives in local storage on node pve"
```
```json
{"name": "list_backups", "arguments": {"node": "pve", "storage": "local"}}
```

### create_backup
```
Agent  : "Back up VM 100 on node pve to local storage"
Agent  : "Create a compressed snapshot backup of VM 100"
```
```json
{"name": "create_backup", "arguments": {"node": "pve", "vmid": 100, "storage": "local"}}
{"name": "create_backup", "arguments": {
    "node": "pve", "vmid": 100, "storage": "local",
    "mode": "snapshot", "compress": "zstd"
}}
```

### restore_backup
```
Agent  : "Restore VM 100 from backup archive local:backup/vzdump-qemu-100-2024_01_15.vma.zst on node pve"
```
```json
{"name": "restore_backup", "arguments": {
    "node": "pve",
    "vmid": 100,
    "type": "qemu",
    "volid": "local:backup/vzdump-qemu-100-2024_01_15-03_00_01.vma.zst",
    "storage": "local-lvm",
    "force": true
}}
```

---

## Clone & Provisioning

### vm_clone
```
Agent  : "Clone VM 100 on node pve to a new VM with ID 101"
Agent  : "Make a full clone of VM 100 named web-server-2"
```
```json
{"name": "vm_clone", "arguments": {"node": "pve", "vmid": 100, "type": "qemu", "newid": 101}}
{"name": "vm_clone", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu",
    "newid": 101, "name": "web-server-2", "full": true
}}
{"name": "vm_clone", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu",
    "newid": 102, "name": "web-staging", "target": "pve2"
}}
```

### vm_create
```
Agent  : "Create a new VM with ID 150 on node pve, 2 cores and 2GB RAM"
Agent  : "Create a new LXC container with ID 200 using the Debian 12 template"
```
```json
{"name": "vm_create", "arguments": {
    "node": "pve", "vmid": 150, "type": "qemu",
    "name": "new-server", "cores": 2, "memory": 2048,
    "storage": "local-lvm"
}}
{"name": "vm_create", "arguments": {
    "node": "pve", "vmid": 200, "type": "lxc",
    "name": "debian-ct", "cores": 1, "memory": 512,
    "storage": "local-lvm",
    "ostemplate": "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
    "disk_size": "8G"
}}
```

### vm_delete
```
Agent  : "Delete VM 150 on node pve"
```
```json
{"name": "vm_delete", "arguments": {"node": "pve", "vmid": 150, "type": "qemu"}}
```

### vm_resize_disk
```
Agent  : "Expand the scsi0 disk of VM 100 on node pve by 20GB"
Agent  : "Set the root disk of container 200 to 20GB"
```
```json
{"name": "vm_resize_disk", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu",
    "disk": "scsi0", "size": "+20G"
}}
{"name": "vm_resize_disk", "arguments": {
    "node": "pve", "vmid": 200, "type": "lxc",
    "disk": "rootfs", "size": "20G"
}}
```

---

## Disk Management

### vm_move_disk
```
Agent  : "Move the scsi0 disk of VM 100 from local-lvm to nas storage"
```
```json
{"name": "vm_move_disk", "arguments": {
    "node": "pve", "vmid": 100,
    "disk": "scsi0", "storage": "nas", "delete": true
}}
```

### vm_unlink_disk
```
Agent  : "Remove the ide2 (CD-ROM) from VM 100 on node pve"
Agent  : "Detach and permanently delete disk scsi1 from VM 100"
```
```json
{"name": "vm_unlink_disk", "arguments": {"node": "pve", "vmid": 100, "idlist": "ide2"}}
{"name": "vm_unlink_disk", "arguments": {
    "node": "pve", "vmid": 100, "idlist": "scsi1", "force": true
}}
```

### list_node_disks
```
Agent  : "List all physical disks installed on node pve"
```
```json
{"name": "list_node_disks", "arguments": {"node": "pve"}}
```

### vm_template
```
Agent  : "Convert VM 100 on node pve into a template"
```
```json
{"name": "vm_template", "arguments": {"node": "pve", "vmid": 100}}
```

---

## Firewall

### list_firewall_rules
```
Agent  : "Show all cluster-wide firewall rules"
Agent  : "Show firewall rules for node pve"
Agent  : "Show firewall rules for VM 100 on node pve"
```
```json
{"name": "list_firewall_rules", "arguments": {"level": "cluster"}}
{"name": "list_firewall_rules", "arguments": {"level": "node", "node": "pve"}}
{"name": "list_firewall_rules", "arguments": {
    "level": "vm", "node": "pve", "vmid": 100, "type": "qemu"
}}
```

### create_firewall_rule
```
Agent  : "Add a firewall rule to allow HTTP traffic to VM 100 on node pve"
Agent  : "Block all incoming traffic from 10.0.0.0/8 at cluster level"
```
```json
{"name": "create_firewall_rule", "arguments": {
    "level": "vm", "node": "pve", "vmid": 100, "type": "qemu",
    "action": "ACCEPT", "direction": "in",
    "proto": "tcp", "dport": "80", "comment": "Allow HTTP"
}}
{"name": "create_firewall_rule", "arguments": {
    "level": "cluster",
    "action": "DROP", "direction": "in",
    "source": "10.0.0.0/8", "comment": "Block RFC1918 inbound"
}}
```

### delete_firewall_rule
```
Agent  : "Delete firewall rule at position 0 for VM 100 on node pve"
```
```json
{"name": "delete_firewall_rule", "arguments": {
    "level": "vm", "node": "pve", "vmid": 100, "type": "qemu", "pos": 0
}}
```

### list_firewall_aliases
```
Agent  : "Show all firewall IP aliases defined in the cluster"
```
```json
{"name": "list_firewall_aliases", "arguments": {}}
```

### list_firewall_ipsets
```
Agent  : "List all firewall IP sets"
```
```json
{"name": "list_firewall_ipsets", "arguments": {}}
```

---

## Historical Metrics — RRD

### node_rrddata
```
Agent  : "Show CPU and memory usage for node pve over the last hour"
Agent  : "Show network I/O for node pve over the last day"
```
```json
{"name": "node_rrddata", "arguments": {"node": "pve"}}
{"name": "node_rrddata", "arguments": {"node": "pve", "timeframe": "day"}}
{"name": "node_rrddata", "arguments": {"node": "pve", "timeframe": "week"}}
```

### vm_rrddata
```
Agent  : "Show CPU history of VM 100 on node pve for the last month"
```
```json
{"name": "vm_rrddata", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
{"name": "vm_rrddata", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu", "timeframe": "month"
}}
```

---

## High Availability

### cluster_status
```
Agent  : "What is the HA and quorum status of the cluster?"
```
```json
{"name": "cluster_status", "arguments": {}}
```

### ha_resources
```
Agent  : "Which resources are managed by the HA manager?"
```
```json
{"name": "ha_resources", "arguments": {}}
```

### ha_groups
```
Agent  : "List all HA groups and their node priorities"
```
```json
{"name": "ha_groups", "arguments": {}}
```

---

## QEMU Guest Agent

> Requires `apt install qemu-guest-agent` inside the VM and the agent service running.

### vm_agent_exec
```
Agent  : "Run 'df -h' inside VM 100 on node pve"
Agent  : "Check the uptime of VM 100 via guest agent"
Agent  : "What processes are running in VM 100?"
```
```json
{"name": "vm_agent_exec", "arguments": {"node": "pve", "vmid": 100, "command": "df -h"}}
{"name": "vm_agent_exec", "arguments": {"node": "pve", "vmid": 100, "command": "uptime"}}
{"name": "vm_agent_exec", "arguments": {"node": "pve", "vmid": 100, "command": "ps aux --no-header"}}
{"name": "vm_agent_exec", "arguments": {"node": "pve", "vmid": 100, "command": "free -m"}}
{"name": "vm_agent_exec", "arguments": {"node": "pve", "vmid": 100, "command": "cat /etc/os-release"}}
```

### vm_agent_info
```
Agent  : "What OS is running inside VM 100?"
```
```json
{"name": "vm_agent_info", "arguments": {"node": "pve", "vmid": 100}}
```

### vm_agent_network
```
Agent  : "What IP addresses does VM 100 have? (from inside the OS)"
```
```json
{"name": "vm_agent_network", "arguments": {"node": "pve", "vmid": 100}}
```

---

## Backup Jobs

### list_backup_jobs
```
Agent  : "Show all scheduled backup jobs in the cluster"
```
```json
{"name": "list_backup_jobs", "arguments": {}}
```

### prune_backups
```
Agent  : "Delete old backups from local storage on pve, keep the last 3 and 7 daily"
Agent  : "Show what backups would be pruned from local storage (dry run)"
```
```json
{"name": "prune_backups", "arguments": {
    "node": "pve", "storage": "local",
    "keep_last": 3, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 3
}}
{"name": "prune_backups", "arguments": {
    "node": "pve", "storage": "local", "vmid": 100,
    "keep_last": 5
}}
{"name": "prune_backups", "arguments": {"node": "pve", "storage": "local"}}
```

---

## Replication

### list_replication
```
Agent  : "List all active replication jobs in the cluster"
```
```json
{"name": "list_replication", "arguments": {}}
```

### create_replication
```
Agent  : "Set up replication of VM 100 to node pve2 every 15 minutes"
```
```json
{"name": "create_replication", "arguments": {
    "id": "100-0",
    "target": "pve2",
    "schedule": "*/15",
    "comment": "Replicate web server to secondary node"
}}
```

### delete_replication
```
Agent  : "Delete replication job 100-0"
```
```json
{"name": "delete_replication", "arguments": {"id": "100-0"}}
{"name": "delete_replication", "arguments": {"id": "100-0", "force": true}}
```

---

## Ceph

### ceph_status
```
Agent  : "What is the current Ceph cluster status?"
```
```json
{"name": "ceph_status", "arguments": {"node": "pve"}}
```

### ceph_health
```
Agent  : "Are there any Ceph health warnings or errors?"
```
```json
{"name": "ceph_health", "arguments": {"node": "pve"}}
```

### ceph_osds
```
Agent  : "List all Ceph OSDs and their status"
```
```json
{"name": "ceph_osds", "arguments": {"node": "pve"}}
```

### ceph_pools
```
Agent  : "Show Ceph storage pools with usage and I/O stats"
```
```json
{"name": "ceph_pools", "arguments": {"node": "pve"}}
```

---

## Node — OS & System

### node_apt_updates
```
Agent  : "Are there any package updates available on node pve?"
```
```json
{"name": "node_apt_updates", "arguments": {"node": "pve"}}
```

### node_syslog
```
Agent  : "Show the last 50 syslog lines from node pve"
```
```json
{"name": "node_syslog", "arguments": {"node": "pve"}}
{"name": "node_syslog", "arguments": {"node": "pve", "limit": 50}}
```

### node_dns
```
Agent  : "What DNS servers is node pve using?"
```
```json
{"name": "node_dns", "arguments": {"node": "pve"}}
```

### node_subscription
```
Agent  : "Is node pve's Proxmox subscription active?"
```
```json
{"name": "node_subscription", "arguments": {"node": "pve"}}
```

### node_reboot
```
Agent  : "Reboot node pve"
```
```json
{"name": "node_reboot", "arguments": {"node": "pve"}}
```

### node_shutdown
```
Agent  : "Shut down node pve"
```
```json
{"name": "node_shutdown", "arguments": {"node": "pve"}}
```

### node_apt_upgrade
```
Agent  : "Refresh the package list on node pve and show available upgrades"
```
```json
{"name": "node_apt_upgrade", "arguments": {"node": "pve"}}
```

### node_certificates
```
Agent  : "Show the TLS certificate details for node pve"
```
```json
{"name": "node_certificates", "arguments": {"node": "pve"}}
```

---

## Users & Access Control

### list_users
```
Agent  : "List all users configured in Proxmox"
```
```json
{"name": "list_users", "arguments": {}}
```

### list_tokens
```
Agent  : "Show all API tokens for user root@pam"
```
```json
{"name": "list_tokens", "arguments": {"userid": "root@pam"}}
{"name": "list_tokens", "arguments": {"userid": "admin@pve"}}
```

### list_acl
```
Agent  : "Show all access control rules in the cluster"
```
```json
{"name": "list_acl", "arguments": {}}
```

### list_pools
```
Agent  : "List all resource pools and their members"
```
```json
{"name": "list_pools", "arguments": {}}
```

---

## Software Defined Networking

### list_vnets
```
Agent  : "What SDN virtual networks are defined?"
```
```json
{"name": "list_vnets", "arguments": {}}
```

### list_sdn_zones
```
Agent  : "List all SDN zones in the cluster"
```
```json
{"name": "list_sdn_zones", "arguments": {}}
```

---

## Notifications

### list_notification_endpoints
```
Agent  : "What notification endpoints are configured?"
```
```json
{"name": "list_notification_endpoints", "arguments": {}}
```

### list_notification_matchers
```
Agent  : "Show notification routing rules"
```
```json
{"name": "list_notification_matchers", "arguments": {}}
```

---

## Console

### vm_console_url
```
Agent  : "Give me the browser console URL for VM 100 on node pve"
Agent  : "Open a console for container 200 on node pve"
```
```json
{"name": "vm_console_url", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
{"name": "vm_console_url", "arguments": {"node": "pve", "vmid": 200, "type": "lxc"}}
```

---

## Reversible Lifecycle Actions

### vm_start
```
Agent  : "Start VM 100 on node pve"
```
```json
{"name": "vm_start", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```

### vm_stop
```
Agent  : "Force stop VM 100 on node pve"
```
```json
{"name": "vm_stop", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```

### vm_shutdown
```
Agent  : "Gracefully shut down container 200 on node pve"
```
```json
{"name": "vm_shutdown", "arguments": {"node": "pve", "vmid": 200, "type": "lxc"}}
```

### vm_reboot
```
Agent  : "Reboot VM 100 on node pve"
```
```json
{"name": "vm_reboot", "arguments": {"node": "pve", "vmid": 100, "type": "qemu"}}
```

---

## Persistent State Changes ⚠

### create_snapshot
```
Agent  : "Take a snapshot of VM 100 on node pve called before-update"
```
```json
{"name": "create_snapshot", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu",
    "name": "before-update", "description": "Pre-upgrade snapshot 2024-01-15"
}}
```

### delete_snapshot
```
Agent  : "Delete snapshot before-update from VM 100 on node pve"
```
```json
{"name": "delete_snapshot", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu", "name": "before-update"
}}
```

### rollback_snapshot
```
Agent  : "Roll back VM 100 on node pve to snapshot before-update"
```
```json
{"name": "rollback_snapshot", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu", "name": "before-update"
}}
```

### vm_migrate
```
Agent  : "Migrate VM 100 from node pve to node pve2"
Agent  : "Live migrate VM 100 to node pve2 without downtime"
```
```json
{"name": "vm_migrate", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu", "target": "pve2"
}}
{"name": "vm_migrate", "arguments": {
    "node": "pve", "vmid": 100, "type": "qemu",
    "target": "pve2", "online": true
}}
```

---

## Multi-step agent conversation examples

```
>>> Which VMs on node pve are using more than 50% CPU?
>>> Show me the config of the VM with the highest memory usage
>>> Take a snapshot of VM 100 called pre-maintenance, then shut it down gracefully
>>> Clone VM 100 to ID 101, start it, and show me its IP via guest agent
>>> List all backups in local storage and delete the ones older than the last 3
>>> Show me the Ceph health status, then list any OSD that is down
>>> Which node has the least CPU load? Migrate VM 100 there.
```
