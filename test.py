#https://pve.proxmox.com/pve-docs/api-viewer/index.html
import os
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
import util
import json
load_dotenv()
PROXMOX_HOST = os.getenv("PROXMOX_HOST")
PROXMOX_PORT = os.getenv("PROXMOX_PORT")
PROXMOX_USER = os.getenv("PROXMOX_USER")
PROXMOX_PASSWORD = os.getenv("PROXMOX_PASSWORD")
PROXMOX_VERIFY_SSL = os.getenv("PROXMOX_VERIFY_SSL").lower()

proxmox = ProxmoxAPI(
    PROXMOX_HOST, port=PROXMOX_PORT, user=PROXMOX_USER, password=PROXMOX_PASSWORD, verify_ssl=False
)

for node in proxmox.nodes.get():
    print(json.dumps(node, indent=4))
    print(f"""Node: {node['node']}
        {node['status']} 
        CPU: {util.decimaltopercentage(node['cpu'])}
        Memory: {util.bytes_to_human_readable(node['mem'])} 
        MaxMemory: {util.bytes_to_human_readable(node['maxmem'])} 
        Disk: {util.bytes_to_human_readable(node['disk'])}
        Naxdisk: {util.bytes_to_human_readable(node['maxdisk'])} 
        Uptime: {util.second_to_human_readable(node['uptime'])}
        """)
    # print(f"Node: {node['node']} => {node['status']}")
    
    # for vm in proxmox.nodes(node["node"]).qemu.get():
    #     print(f"{vm['vmid']}. {vm['name']} => {vm['status']}")
    #     print(vm)
    # for vm in proxmox.nodes(node["node"]).lxc.get():
    #     print(f"{vm['vmid']}. {vm['name']} => {vm['status']}")

for storage in proxmox.storage.get():
    print(json.dumps(storage, indent=4))
    print(f"""Storage: {storage['storage']})
        {storage['type']}
        {storage['content']}
        """)
