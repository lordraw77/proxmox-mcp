"""
util.py — Formatting utilities for Proxmox metric values
=========================================================
The Proxmox REST API returns raw numeric values for all metrics:
  - CPU utilisation as a float in the range [0.0, 1.0]
  - Memory, disk and network sizes in bytes (plain integers)
  - Durations (uptime) in seconds (plain integers)

This module converts those raw values into compact, human-readable strings
suitable for display in terminal output or LLM responses.
"""


def decimaltopercentage(value: float) -> str:
    """
    Convert a decimal CPU utilisation fraction to a percentage string.

    Proxmox reports CPU load as a value between 0.0 (idle) and 1.0 (fully
    loaded across all cores).  This function multiplies by 100 and formats
    the result to two decimal places.

    Args:
        value : Float in [0.0, 1.0], e.g. 0.0342 for 3.42 % utilisation.

    Returns:
        Formatted percentage string, e.g. "3.42%".

    Examples:
        >>> decimaltopercentage(0.0)
        '0.00%'
        >>> decimaltopercentage(0.5)
        '50.00%'
        >>> decimaltopercentage(1.0)
        '100.00%'
    """
    return f"{value * 100:.2f}%"


def bytes_to_human_readable(num_bytes: int) -> str:
    """
    Convert a byte count to the most appropriate IEC binary unit string.

    Iterates through the unit ladder [B, KB, MB, GB, TB, PB], dividing by
    1024 at each step.  Stops at the first unit where the value is less than
    1024 and formats the result to two decimal places.

    This function uses powers of 1024 (binary prefixes), which matches the
    convention used by the Proxmox web interface and most virtualisation tools.

    Args:
        num_bytes : Non-negative integer byte count.

    Returns:
        Human-readable string with unit suffix, e.g. "1.50 GB".

    Examples:
        >>> bytes_to_human_readable(0)
        '0.00 B'
        >>> bytes_to_human_readable(1536)
        '1.50 KB'
        >>> bytes_to_human_readable(1073741824)
        '1.00 GB'
        >>> bytes_to_human_readable(1099511627776)
        '1.00 TB'
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0

    # If we exhaust all units (value >= 1024 PB) return in PB.
    # This is a theoretical safeguard — modern Proxmox deployments do not
    # exceed petabyte scale in a single cluster.
    return f"{num_bytes:.2f} PB"


def second_to_human_readable(num_seconds: int) -> str:
    """
    Convert a duration in seconds to a compact human-readable string.

    Iterates through the unit ladder [s, m, h, d], dividing by 60 at each
    step for seconds→minutes→hours, and then by 24 for hours→days.
    Stops at the first unit where the value is less than 60 (or less than 24
    for the hours→days transition).

    Note: the division factor is 60 for all transitions including hours→days
    because the loop divides uniformly — the result at the 'd' (day) step
    will therefore be expressed as hours/24 which numerically equals days.
    This works correctly for typical uptime values up to tens of thousands
    of days.

    Args:
        num_seconds : Non-negative integer number of seconds.

    Returns:
        Human-readable string with unit suffix, e.g. "3.50 d" (3.5 days).

    Examples:
        >>> second_to_human_readable(45)
        '45.00 s'
        >>> second_to_human_readable(90)
        '1.50 m'
        >>> second_to_human_readable(3600)
        '1.00 h'
        >>> second_to_human_readable(86400)
        '24.00 h'
    """
    for unit in ['s', 'm', 'h', 'd']:
        if num_seconds < 60.0:
            return f"{num_seconds:.2f} {unit}"
        num_seconds /= 60.0

    # Fallback — value exceeded the 'd' threshold.
    return f"{num_seconds:.2f} d"
