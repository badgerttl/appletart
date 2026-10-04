"""Update and install software through a detected native package manager."""

import shlex
from .bundles import packages

LINUX_BUILD_NOTICE = "Initial Linux builds update package indexes and upgrade installed packages, even with no bundles selected. Updates and software installation can take 5–15 minutes or longer. Guest provisioning allows 30 minutes and requires working package repositories."


def install_script(selection, family, *, upgrade=False):
    selection = packages(list(selection))
    upgrade = upgrade and family == "linux"
    if not selection and not upgrade:
        return ""
    arguments = shlex.join(selection)
    if family == "macos":
        return f"""\nset -eu
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH
command -v brew >/dev/null || {{ echo 'Software bundles require Homebrew in this macOS template. Choose a base or Xcode image, or install Homebrew first.' >&2; exit 1; }}
test "$(id -u)" != 0 || {{ echo 'Homebrew provisioning requires the configured login account.' >&2; exit 1; }}
brew install -- {arguments}
"""
    providers = (
        ("apt-get", "apt-get update",
         "env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get upgrade -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold",
         "env DEBIAN_FRONTEND=noninteractive apt-get install -y"),
        ("dnf", "dnf makecache --refresh", "dnf upgrade -y", "dnf install -y"),
        ("yum", "yum makecache", "yum update -y", "yum install -y"),
        ("zypper", "zypper --non-interactive refresh", "zypper --non-interactive update", "zypper --non-interactive install"),
        ("apk", "apk update", "apk upgrade", "apk add"),
        # Pacman refreshes and upgrades together to avoid partial upgrades.
        ("pacman", "", "pacman --noconfirm -Syu", "pacman --noconfirm -S"),
    )
    script = ["set -eu"]
    for index, (manager, refresh, update, install) in enumerate(providers):
        script.append(f"{'if' if index == 0 else 'elif'} command -v {manager} >/dev/null; then")
        if upgrade:
            script.append(f"    echo 'Updating package indexes and upgrading installed packages with {manager}…'")
            if refresh:
                script.append(f"    sudo -n {refresh}")
            script.append(f"    sudo -n {update}")
            script.append("    echo 'System package upgrade completed.'")
        elif manager == "apt-get":
            script.append(f"    sudo -n {refresh}")
        if selection:
            script.append("    echo 'Installing selected software…'")
            script.append(f"    sudo -n {install} -- {arguments}")
    script.extend(["else", "    echo 'No supported native package manager was detected for guest provisioning.' >&2", "    exit 1", "fi"])
    return "\n".join(script) + "\n"
