# AppleTart

AppleTart is a local browser dashboard and command-line tool for Linux and macOS
virtual machines on Apple Silicon, powered by Tart. It manages image downloads,
VM provisioning, networking, guest access, reusable images, and recovery from
one workspace.

You can:

- Create VMs from published Tart images, stopped local templates, ARM64 cloud
  disks, saved golden images, or Linux installer ISOs.
- Provision guest accounts, SSH keys, passwords, and optional software bundles.
- Start, gracefully shut down, restart, configure, and destroy managed VMs.
- Use NAT or bridged networking, forward TCP/UDP services, and share Mac folders.
- Manage guests through a privileged VSOCK agent, with SSH fallback.
- Save deployment profiles, prepare golden images, and create disk checkpoints.
- Inspect health and persistent operation logs, cancel builds, resume incomplete
  setup, and clean up unused storage.

Windows guests are not supported. Linux defaults to headless startup; macOS
opens a Tart display by default. There is no embedded browser console.

## Install and launch

Requirements:

- An Apple Silicon Mac running macOS 13 or later.
- Native ARM64 Python 3.11 or later and Tart on your `PATH`.
- For cloud disk conversion, `qemu-img` (from Homebrew's `qemu` package).
  Cloud-init seed creation also uses macOS's built-in `hdiutil`.

From this project directory:

```sh
brew install openai/tools/tart qemu
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m appletart
```

The Python install includes PyYAML for guest-user imports. You can also
double-click **AppleTart.command** in Finder; it uses this project's `.venv`
when available, otherwise `python3` on your `PATH`.

The dashboard opens at [http://127.0.0.1:4991](http://127.0.0.1:4991) and runs
as a detached background process. The command returns once it is ready;
you can close the terminal and browser. Running the launcher again with the
same data directory reopens the existing dashboard. Finder's **AppleTart.command**
uses this behavior too.
The process inherits the launcher's local-network access, so SSH health checks
work when started from Terminal or Finder. Status and stop commands verify the
process identity before using its saved PID.
The service runs for the current login session; launch it again after logging
in or rebooting. It does not automatically start at login.

```sh
.venv/bin/python -m appletart service status
.venv/bin/python -m appletart service stop
.venv/bin/python -m appletart service start
.venv/bin/python -m appletart service restart

# Keep output attached to a terminal for debugging.
.venv/bin/python -m appletart ui --foreground
```

Stopping the service cancels active cancellable operations and waits for
cleanup; already deployed VMs remain in Tart. Service commands accept
`--data-dir`; use the same directory as the dashboard you want to manage.
Output is saved to `DATA_DIR/service/dashboard.log`, also shown by `service status`.
`service start` and `ui` both start the background dashboard and open its URL.
Use `--no-browser` to start or reuse it without opening a browser. `service status`
reports whether the process is running, its PID, URL when ready, and log path.
To change the port of an existing service, use `service restart --port PORT`
with the same `--data-dir`. The new process uses the Python environment and
`PATH` from the command that starts it.
Stop any previously launched foreground dashboard with Ctrl+C before using the
background launcher with that same data directory.

`appletart start NAME` starts a VM and requires its name. To start the dashboard,
use `appletart service start` or `appletart ui`. Likewise, `service stop` stops
the dashboard; `stop NAME` terminates a VM.

```sh
# Check host requirements.
.venv/bin/python -m appletart doctor

# Use another port without opening a browser; port 0 chooses an available port.
.venv/bin/python -m appletart ui --port 4992 --no-browser
```

The service prints its exact URL. It listens only on `127.0.0.1`, uses a
per-session request token, and rejects requests from other origins. Open it on
the Mac hosting the VMs. Guest service forwarding has its own listen addresses
and ports.

### Choose the application data directory

The default is `~/Library/Application Support/AppleTart`. Override it with
`APPLETART_HOME` or `--data-dir`. The Finder launcher uses the same default;
it does not automatically select a project-local data directory.

This workspace's existing VM records, including `houseofpain`, are under
`appletart/.appletart`. From the project root, use these commands:

```sh
.venv/bin/python -m appletart service start --data-dir appletart/.appletart
.venv/bin/python -m appletart service status --data-dir appletart/.appletart
.venv/bin/python -m appletart service stop --data-dir appletart/.appletart
.venv/bin/python -m appletart service restart --data-dir appletart/.appletart
```

`./.appletart` and `appletart/.appletart` are different directories. A dashboard
opened with `./.appletart` cannot see the records in `appletart/.appletart` and
may label those VMs **Created outside AppleTart. Managed in Tart.** Relative
paths are resolved from the directory where you run the command. Use an
absolute path when launching from another directory.

To make subsequent commands in the current shell use these existing records:

```sh
export APPLETART_HOME="$PWD/appletart/.appletart"
.venv/bin/python -m appletart service start
```

Run the export from the project root. It applies to that shell and its child
processes; a Finder launch uses its own environment.

Use the same data directory for dashboard and CLI commands. It contains VM
records, settings, profiles, golden-image metadata, downloads, converted disks,
cloud-init seeds, host-key records, and logs. Tart owns VM disks separately and
honors `TART_HOME`. VMs without records in the selected application directory
appear as read-only external entries. AppleTart checks the directory identity
of managed VMs before changing or deleting them.

### Startup troubleshooting

- **Port already in use:** startup reports the conflict immediately. Reopen the
  running dashboard with its data directory, stop that service using the same
  directory, or select a different port with `--port`. Changing `--data-dir`
  does not change the default port; two dashboards cannot both listen on 4991.
- **Another service command is still running:** a start or stop command already
  holds the control lock for that directory. Wait for it to finish or interrupt
  that command with Ctrl+C, then retry. Startup does not silently wait on the lock.
- **Dashboard exited during startup / did not become ready:** inspect
  `DATA_DIR/service/dashboard.log`. An exited process reports its exit code and
  available startup output immediately; a process still starting has a 20-second readiness
  limit. Address the reported cause before retrying.

For this workspace's existing records, inspect the dashboard log with:

```sh
tail -n 50 appletart/.appletart/service/dashboard.log
```

For the shorter `appletart` commands below, activate the environment first:

```sh
source .venv/bin/activate
```

## Create a VM

Choose **New Virtual Machine** and complete the four steps:

1. **Image:** select a platform, a defined version or variant, a VM name, and an
   image type. Cloud disks and ISOs accept HTTPS downloads or local files; use
   **Browse…** for the native Mac file chooser. Local files are used directly.
   Supply an optional publisher SHA-256 or SHA-512 checksum.
2. **Resources:** choose a preset and optionally override CPU, memory, and disk.
3. **Networking And Access:** choose NAT or bridged interfaces, forwarding rules,
   directory shares, the guest account, SSH keys, optional passwords, additional
   users, and software bundles.
4. **Review:** download only, build only, or build and deploy. You can also save
   a deployment profile and choose whether to run headless.

| Preset | CPU cores | RAM | Disk capacity |
| --- | --- | --- | --- |
| Small | 2 | 4 GB | 40 GB |
| Medium | 4 | 8 GB | 80 GB |
| Large | 8 | 16 GB | 160 GB |

CPU requests cannot exceed the host CPU count. Disk sizes use decimal GB and
can only grow. Catalog minimums and larger source disks override smaller
requests; macOS base images have a 50 GB minimum, and many Xcode variants need
140 GB. Tart checks resource feasibility. Guests may need partition/filesystem
expansion after later disk growth.

### Supported sources and the image catalog

| Platform | Choices in the shipped catalog | Setup |
| --- | --- | --- |
| Ubuntu | 24.04 / 22.04 Tart templates | Clone and provision |
| Debian | trixie Tart template | Clone and provision |
| Fedora | 42 Tart template | Clone and provision |
| Rocky Linux | 9 Tart template | Clone and provision |
| Ubuntu Runner | ARM64 24.04 / 22.04 Tart templates | Clone and provision |
| macOS | Golden Gate, Tahoe, Sequoia, Sonoma, Ventura, Monterey; base, vanilla, Xcode, and runner variants where defined | Clone a Tart template |
| Kali | Defined 2026.2 ARM64 generic cloud image and checksum | Unattended cloud-init |
| RHEL | Your compatible ARM64 cloud disk | Unattended cloud-init |
| Other Linux | Your compatible cloud disk, Tart template, or installer ISO | Depends on source type |

These are the definitions bundled in [appletart/data/images.json](appletart/data/images.json),
not a live discovery of publisher releases. The newest guest OS must also be
supported by the host's macOS/Virtualization framework. macOS provisioning
requires a Tart template; cloud and golden-image provisioning require Linux.

Use **Settings → Image Catalog → Add version**, or **Add image version** in the
wizard, to save a source and optional checksum. Adding a version does not
download or boot it and preserves the current default. **Edit catalog** edits
platform definitions and versions as JSON, saved in `image-catalog.json` in the
application data directory. Definitions include the guest family, icon,
bootstrap login, and minimum disk size.

Catalog bootstrap passwords are public template defaults stored in plain text.
Every template build installs and verifies privileged management before retiring
the bootstrap password, even when no SSH keys or software are selected.
Use the wizard's password fields for private credentials. OCI tags can change;
use pinned tags or digests when repeatability matters. Remote cloud disks and
ISOs are cached; **Download again** or `--refresh-source` fetches them again and
still checks any supplied checksum.

Cloud sources support `.tar.xz` containing one regular raw/QCOW2 disk,
`.qcow2.xz`, `.qcow2`, `.raw`, and `.img`. AppleTart detects the actual disk
format, converts it to raw, rejects QCOW2 external backing/data files, and
imports it into Tart. Disks must support ARM64 UEFI, virtio, and cloud-init's
NoCloud datasource. VMware/VirtualBox downloads are not Tart templates.

### Provisioning and software

Fresh Linux cloud builds use a NoCloud seed to configure the hostname,
non-root sudo account, keys, DHCP, filesystem growth, packages, and guest agent.
Linux Tart templates are provisioned through an existing SSH login. For an ISO,
complete the installation in Tart, enable SSH, shut down the guest, and choose
**Finish setup**.

Initial Linux provisioning refreshes package indexes and upgrades installed
packages even when no additional software is selected. It supports `apt-get`,
`dnf`, `yum`, `zypper`, `apk`, and `pacman` on templates and installed guests;
cloud builds use cloud-init's package integration. Working repositories are
required. RHEL images may need registration and enabled repositories first.
Updates and software installation can take **5–15 minutes or longer**.

Fresh cloud builds allow up to 30 minutes for SSH readiness, then a separate
30 minutes for cloud-init setup. Template SSH readiness normally allows 120
seconds, with up to 30 minutes for provisioning. Builds stop their setup guest
when finished; **Build only** leaves it stopped and ready to start.

**Settings → Software Bundles** lets you create, edit, or delete named bundles
of native package names for compatible platforms. Select bundles and optional
additional packages in the wizard. Selections are merged and deduplicated;
new VMs have no bundles selected automatically. **Kali essentials** is an
optional editable bundle. macOS software selections use Homebrew as the
configured login account; a vanilla template without Homebrew can be cloned
with no software selections.

Golden clones reuse installed software without another full system upgrade;
repositories are refreshed when additional packages are requested. Bundle
changes affect future deployments. Existing VM records retain their resolved
package snapshot for retries. A bundle referenced by a saved deployment profile
cannot be deleted until that profile is updated or removed. Selecting packages
does not uninstall software already in the source image.

Cloud provisioning requires cloud-init with NoCloud, systemd, OpenSSH, sudo,
Bash, iproute2, and Linux account tools. Golden preparation checks these
capabilities on the copy before saving it. VM details retain both the requested
image release and the OS/version observed inside the guest.

## Guest access and users

In **Settings → SSH Connections**, select Terminal, iTerm2, or Warp, set a
local `.pub` path, and choose **Install SSH Public Key By Default**. Only
installed terminals are enabled. The default key choice applies to new VMs and
quick golden-image launches; saved profiles keep their own selections.

The wizard's **Install SSH Public Key** toggle accepts multiple `.pub` paths
or disables personal key installation. Private keys stay on the Mac. Cloud and
golden builds still create the management account and verify the privileged
agent with this toggle off. Fresh cloud builds use a temporary verification key
and remove it after successful setup. Failed builds retain it privately for
retry. Disabling personal keys does not disable the guest SSH service.

For Tart templates and ISO setup, supply an existing guest login. **Template
Login Password** authenticates setup; **Guest Password** sets the account's
password and enables SSH password login. Matching published templates can use
their catalog bootstrap login. After template management is verified, Linux
removes the bootstrap account's password hash; macOS replaces its password with
a random credential that is not retained. AppleTart then applies any explicitly
selected guest password. Select a guest password if you need password or
graphical login on macOS; SSH keys and agent management remain available.

The **SSH** action opens your chosen terminal with the saved username, current
IP, identities, and per-VM host-key file. **Details → Copy SSH command** provides
the command. **Add To SSH Config** creates or updates a marked block in
`~/.ssh/config`, so you can use `ssh VM_NAME`. Repeat it after changing the IP
or SSH settings. Unmarked conflicting aliases are preserved and reported;
changed files are backed up under `~/.ssh/appletart-backups`.

### Privileged guest agent

Provisioned guests use a pinned Tart guest agent (currently 0.15.0) for IP
discovery and privileged command execution over VSOCK. It does not need a
LAN management port, a guest IP, or personal SSH authentication. Linux runs
it as a root service; macOS uses a root launchd service while preserving the
publisher's clipboard agent.

Resuming an incomplete cloud build tries its installed privileged agent before
SSH. This also recovers a build whose temporary SSH key was already revoked
before its final seed or saved configuration could be written.

Guest-user management, orderly shutdown, detailed interface/mount checks, and
log collection prefer the agent and use saved SSH access as a fallback.
Choose **Install/Upgrade guest agent** or **Repair guest agent** for an older
VM. Running guests upgrade in place; stopped guests briefly boot on NAT,
verify the agent, and stop again. The repair dialog accepts a transient SSH
fallback password. SSH fallback requires passwordless sudo.

### Additional users

Add users during the wizard's access step or use **Manage Users** on a running
VM. Enter SSH public keys, passwords, or both. **Import YAML** accepts a local
file; **Download YAML Template** supplies an example:

```yaml
version: 1
users:
  - username: developer
    ssh_authorized_keys:
      - "ssh-ed25519 REPLACE_WITH_YOUR_PUBLIC_KEY developer@mac"
```

Every submitted account receives passwordless sudo. Imports append missing
keys to existing regular accounts; key-only new accounts have locked passwords.
Dotted usernames are supported. The importer rejects root/system accounts,
invalid keys, unsafe SSH symlinks, duplicate YAML fields, aliases, and object
tags. Each operation accepts up to 20 accounts and 16 keys per account.

Passwords are transient and excluded from saved VM configurations, profiles,
seeds, and operation logs. Recipes keep public keys and password-login
requirements; retries and deployments request fresh passwords when required.
YAML files containing passwords are plain text. Guest changes already completed
before a failure remain available for retry.

## Networking and shared folders

**NAT** shares the Mac's connection. **Bridged** attaches the guest to selected
Mac interfaces and supports additional adapters. Stock Tart assigns the same
VM MAC to those adapters, so use separate networks. NAT and bridging are
separate modes. Cloud setup temporarily uses NAT, then deploys with the saved
network settings.

The machine's IP button copies its current IPv4 address. Discovery prefers the
agent for bridged VMs and VMs with a recorded agent installation, then falls
back to ARP for bridging or DHCP for NAT. Stopped VMs clear their displayed IP.
Bridging still depends on host permissions, guest DHCP, and the physical LAN.

### Forward guest services

NAT forwarding rules specify a Mac listen IP or interface, host port, guest
port, and TCP/UDP protocol:

```text
Client → Mac IP:host port → Guest NAT IP:service port
```

New rules default to following a Mac interface such as `en0`. The relay updates
its listen address when that interface's IPv4 address changes. Fixed IPs,
`127.0.0.1` for local access, and `0.0.0.0` for all Mac interfaces are also
available. The guest service must listen on its NAT address or `0.0.0.0`.
AppleTart does not change Mac firewall rules or upstream routing.

Listeners are checked before boot; forwarding failures during deployment stop
the VM. Relays remain active when the browser or dashboard closes and stop
when the guest stops. Change rules through **Configure** while the VM is
stopped. Traffic is relayed through the Mac, which the guest sees as its source.

### Share Mac directories

Choose **Browse…** or enter a local folder path, then a guest mount below
`/mnt`, `/media`, or `/srv`. VirtioFS shares default to read-only; read-write
is optional. Paths with spaces work; host paths containing colons do not.

Cloud and golden guests mount shares automatically. **Configure → Directory
Shares** applies changed mappings on their next start while preserving accounts,
host keys, and software. Templates and ISO installations show manual mount
commands in Review, including `mount_virtiofs` for macOS. Shared Mac directories
must exist whenever the VM starts.

## Manage VMs and diagnose problems

The dashboard lists managed and external Tart VMs, with search, refresh,
platform icons, and remembered appearance preferences. **Settings → Appearance**
offers Orchard, Macintosh, Graphite, iMac Grape, Tokyo Night, and Classic Rainbow
palettes, each in light or dark mode. Changes apply immediately and are saved in
this browser. The header’s sun/moon button switches modes while keeping your
palette. Hover over action icons for their names.

| Action | Behavior |
| --- | --- |
| Power toggle | Start or open an installer; switching off requests graceful shutdown |
| Restart | Shut down orderly, then boot with the saved network and display settings |
| Force stop | Confirm immediate termination when graceful shutdown cannot complete |
| Configure | Change resources, networking, and shares while stopped, preserving the MAC |
| Build / Resume build | Continue incomplete setup using its existing disk |
| Details | Inspect saved configuration, guest OS, live health, forwarding, shares, and recovery points |
| Log | Read persistent operation attempts or Tart/serial output |
| Destroy | Require a stopped VM and its exact name before deleting the disk |

Graceful shutdown uses the privileged agent with SSH fallback. If it fails,
the guest stays running and the error is reported; force stop is separate.
Startup reuses the saved headless/display choice. SSH health distinguishes a
responding SSH service from successful key authentication; agent management can
be ready independently of SSH.

**Activity** shows operation progress and cancellation controls. Different VMs
can run operations concurrently; conflicting VM/image operations and shared
cache work are locked, including across dashboard and CLI processes. Builds
preserve completed downloads and partially configured disks on failure or
cancellation. Wait for cleanup, then resume or destroy the stopped incomplete VM.
AppleTart never passes Tart's `--overwrite` and disables automatic pruning.

Operation transcripts persist under `DATA_DIR/logs/operations/VM_NAME/`.
**Detailed log** selects a specific attempt; **Load earlier** and **Latest
output** navigate it. Copy/download exports include the full selected saved log.
**Collect guest logs** captures a running guest through the agent with SSH
fallback, without restarting it. Logs survive dashboard restarts and remain on
disk until manually removed. Activity's copy button exports its received log
history. Readers who scroll up keep their position during live updates.

Transcripts include command arguments, output, exit codes, durations, and
exception traces. Command stdin and environment variables are excluded;
bootstrap passwords are redacted. Guest snapshots include bounded cloud-init,
package-manager, and service logs. If bootstrap access fails, inspect the
retained probes and Tart/serial output.

## Reuse, recovery, and storage

**Images & profiles** groups Deployment Profiles, Golden Images, and Platform
Profiles.

### Deployment profiles

Save a profile from a VM's Details or the wizard's Review step. It retains
resources, source/checksum, packages, networking, forwards, shares, account
recipes, and public-key paths. It excludes passwords and private-key contents.

**Deploy** asks for a new VM name and starts the saved recipe directly;
**Customize** opens the wizard. Each use derives a fresh hostname. Profiles can
be imported/exported as JSON, have notes edited, and be deleted. Imported
profiles cannot overwrite existing ones. Local paths and host interfaces must
be valid on the current Mac. ISO deployments still need manual installation;
Deploy prompts for fresh account passwords when a recipe requires them.
Use Customize when a template needs a setup password.

To change the saved recipe, choose **Edit profile** in **Images & profiles →
Deployment Profiles**. The wizard loads its image, resources, networking,
shares, SSH settings, accounts, software, and notes. Review your changes and
choose **Save changes** to update that same profile. Its name stays fixed;
future deployments use the updated recipe, and existing VMs keep their settings.

### Golden images

Stop a completed Linux cloud or Tart VM and choose **Save as golden image**.
AppleTart clones it, verifies cloud-init and agent capabilities on the copy,
and clears instance state, machine ID, authorized keys, passwords, and SSH
host keys. The source VM stays unchanged. This workflow requires a
cloud-init-capable Linux guest; macOS templates and guests without the required
capabilities cannot be saved this way.

Golden clones retain installed software and get a new MAC, cloud-init instance
ID, hostname, and selected account/keys. Provisioning prefers the inherited
privileged agent, with SSH bootstrap fallback. No extra bundles are selected
automatically.

The golden image's **Launch** action asks only for a new VM name, then builds
and starts it with Small resources, NAT, the platform's default username, and
the SSH key preference from Settings. The disk is at least as large as the
image. **Customize** opens the full wizard.

Golden metadata includes version, notes, build date, source/checksum, observed
OS, agent version, and requested packages. The package list is not a complete
installed-software inventory. Save changed disks under new image names.
VM, checkpoint, and profile references block deletion of an image.

### Checkpoints

From a stopped, completed VM's Details, create a named checkpoint with optional
notes. It saves a cloned disk and deployment settings, including seed/instance
information and public SSH host-key records. It does not capture running memory,
firmware/NVRAM, or shared Mac folders.

Checkpoints require a full shutdown. Resume a suspended VM and shut it down
before checkpointing or restoring. **Restore** requires typing the VM name;
AppleTart first saves an automatic recovery point, then restores the disk and
settings while retaining the VM directory and MAC. A failed restore blocks
startup until recovery succeeds. Checkpoint deletion requires its exact name;
remove a VM's checkpoints before destroying the VM.

### Storage cleanup

**Settings → Storage** shows managed disks, checkpoints, golden images,
downloads, converted disks, cached tools, and free space on the data volume.
Allocated sizes are estimates because APFS clones can share blocks. External
VMs and Tart registry caches are excluded.

**Purge unused** reviews and removes unused download, conversion, and tool
caches. **Clean up selected** also supports unreferenced golden images and
requires typing `DELETE`. Managed VM disks, checkpoints, referenced images,
and artifacts needed by incomplete builds are protected. Cleanup rechecks
references and cannot overlap lifecycle work.

## CLI examples

Commands use the default data directory unless you pass `--data-dir` or set
`APPLETART_HOME`. For this workspace's existing records, append
`--data-dir appletart/.appletart` to management commands.

```sh
# Build and start a published template.
appletart create ubuntu-dev --os ubuntu --size medium \
  --ssh-public-key ~/.ssh/id_ed25519.pub

# Unattended cloud setup with a shared folder and interface-following relay.
appletart create kali-lab --os kali --size medium \
  --ssh-user developer --ssh-public-key ~/.ssh/id_ed25519.pub \
  --forward en0:8443:443/tcp --share ~/Projects:/mnt/projects:ro

# Import a local compatible ARM64 cloud disk without a personal SSH key.
appletart create debian-local --os other --source-kind cloud \
  --source ~/Downloads/debian-arm64.qcow2 --ssh-user developer --no-ssh

appletart machines
appletart health kali-lab --details
appletart connect kali-lab --terminal
appletart shutdown kali-lab
appletart start kali-lab
appletart restart kali-lab

# Save recovery and reuse artifacts after shutdown.
appletart shutdown kali-lab
appletart checkpoint kali-lab --label before-upgrade
appletart checkpoints kali-lab
appletart golden kali-lab --image-name kali-golden --image-version 1
appletart profile-save kali-recipe --from-vm kali-lab
appletart profile-export kali-recipe kali-recipe.json
appletart images
appletart storage
```

`download`, `build`, and `create` accept resource overrides, source/checksum,
public keys, networking, repeated `--bundle` / `--package`, and repeated
`--forward` / `--share` options. `create` downloads, builds, and starts; `build`
and `download` can run independently. Reuse the same configuration when
resuming a CLI build. `--ask-password` prompts for bootstrap access.
`finish NAME --ask-password` completes setup after a manual ISO installation.
`start` and `create` accept `--headless` / `--no-headless`.

Use `appletart COMMAND --help` for restore confirmations, checkpoint deletion,
force stop, destruction, profile import/deletion, guest-agent installation,
and storage cleanup arguments. CLI cleanup uses repeated `--cleanup ITEM_ID`
and `--confirm DELETE`.

The original TOML `plan` and `deploy` commands remain available for existing
manifests; they do not create dashboard records. `list`,
`run`, `stop`, and `ip` operate directly through Tart. In particular, `stop`
is direct termination; use `shutdown` for orderly guest power-off.

## Development and verification

```sh
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_live_updates.mjs
```

Python tests cover background-service startup, reuse, port conflicts, control
locks, startup failures, and shutdown, as well as provisioning, image/catalog
validation, bundles, guest capabilities, accounts, SSH trust, agent management,
lifecycle/recovery, networking, profiles, storage protection, diagnostics, and
the local HTTP API.
Frontend tests cover retained controls, launch behavior, file-picker cancellation,
log scrolling/copying, and live updates. The normal suite uses fake backends,
mocked downloads, and a loopback HTTP server; it does not download guest images
or boot VMs.

Historical guest and browser integration evidence is under
[validation-results](validation-results/), including catalog/bundle checks,
cloud/golden lifecycle tests, platform fixes, and launch-latency checks. These
records describe specific past runs rather than guarantees for every publisher
image, host version, or bridged network. A full macOS VM build is not covered
by the catalog/bundle integration run.
