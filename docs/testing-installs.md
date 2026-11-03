# Testing an install

How the code sandbox works on each OS, and step-by-step checks to confirm an install works on yours. Install commands are in the [README](../README.md#install).

**Code sandbox per OS.** Generated code (during validation) and learned MCP tools run without network access:

| OS | Sandbox | Notes |
|---|---|---|
| Linux | `unshare -rn` (user + network namespace) | Needs unprivileged user namespaces. Ubuntu 24.04+ restricts them via AppArmor; if the installer warns, allow them (`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`) or add an AppArmor profile |
| macOS | `sandbox-exec` with a deny-network profile | Built in; Apple marks it deprecated, but it still ships |
| Windows | none | Code refuses to run unless you opt in (the installer asks; `--allow-unsandboxed`). For full isolation install inside **WSL2** with `install.sh` ([WSL](#wsl)) |

Resource limits (CPU, memory, file size) apply on Linux and macOS. The environment is scrubbed and there's a timeout everywhere. The static safety scan rejects network code on every OS.

- [Linux](#linux)
- [macOS](#macos)
- [Windows](#windows)
- [WSL](#wsl)
- [Docker](#docker)

## Linux

Every test runs on Linux. Open a terminal; run the commands from the repo folder (`~/claude-learning-harness`) unless noted.

**1. Prerequisites.** Each should print a version, and Python must be 3.10+:
```bash
python3 --version; git --version; claude --version
```

**2. The sandbox works for your user.** This should print `0`:
```bash
unshare -rn true; echo $?
```
If it fails (`Operation not permitted`), unprivileged user namespaces are disabled:
- **Ubuntu 23.10+ / 24.04+** (AppArmor restriction): `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. To keep it across reboots: `echo 'kernel.apparmor_restrict_unprivileged_userns=0' | sudo tee /etc/sysctl.d/60-userns.conf`
- **Older Debian kernels**: `sudo sysctl -w kernel.unprivileged_userns_clone=1`
- **Inside Docker or another container**: the container's seccomp profile usually blocks it. Run the harness on the host, or start the container with user namespaces allowed.

Without it the installer still works, but validation and MCP tools refuse to run code unless you opt in.

**3. Run the installer.** Run `./install.sh`, or the `curl ... | bash` one-liner from the README. Expected:
- `[ok]` for Python, git and the Claude Code CLI
- `[ok] code sandbox: unshare (network blocked for generated code and tools)`
- `[ok] self-test: OK`
- `[ok]` for the hooks (`~/.claude/settings.json`), the MCP server, and the PATH (`~/.bashrc`, `~/.zshrc`, `~/.config/fish/config.fish` or `~/.profile`, depending on `$SHELL`)

**4. Status.** Open a **new** terminal first (or `source` your rc file). Expect `[ok]` for everything, including the sandbox, `hooks installed`, `MCP server `learned` registered` and `bin/ on PATH`:
```bash
python3 install.py --check
```

**5. MCP server.** Run this outside the repo folder, so the repo's own `.mcp.json` doesn't answer instead. Expect `Scope: User config (available in all your projects)`, `Status: √ Connected` and your Python as the command:
```bash
cd ~ && claude mcp get learned
```

**6. PATH.** Expect a line ending in `claude-learning-harness/learned/bin` (where wrappers for scripts you approve will go), and your Python's path:
```bash
echo "$PATH" | tr ':' '\n' | grep learned/bin
echo "$LEARNING_HARNESS_PYTHON"
```

**7. Session logging.** Start `claude` in any folder, send one message, then exit (`/exit`). Back in the repo folder, the session should appear at the top:
```bash
python3 review/cli.py list -n 3
```
If it doesn't, check the hook command in `~/.claude/settings.json` (the quoted Python path followed by the quoted `hooks/log_session.py` path) and `store/hook_errors.log`.

**8. Tests.** Every test runs on Linux; the suite should end with `OK` and nothing skipped:
```bash
python3 -m unittest tests.test_sandbox -v      # starts with: platform=linux backend=unshare
python3 -m unittest discover -s tests -t .     # the full suite
```

**9. Uninstall.** Expect `[ok]` lines for removing the hooks, the MCP server and the PATH block from your rc file:
```bash
python3 install.py --uninstall
```

If a step fails, please open an issue with the output of steps 2–4 and 8, plus `cat /etc/os-release | head -3` and `uname -r`.

## macOS

On macOS the sandbox is `sandbox-exec` with the profile `(version 1)(allow default)(deny network*)`. Run these from the repo folder:

**1. The sandbox tool exists and accepts the profile.** This should print `0`:
```bash
sandbox-exec -p '(version 1)(allow default)(deny network*)' /usr/bin/true; echo $?
```

**2. It blocks the network, and only inside the sandbox.** Run the same connection without the sandbox first, so a Mac that is simply offline can't look like a pass:
```bash
python3 -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('open')"
sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  python3 -c "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3); print('open')"
```
Expected: the first prints `open`; the second fails with `PermissionError` / `Operation not permitted` (or another `OSError`) instead of printing `open`.

**3. The harness picks it up.** Expected output includes `code sandbox: sandbox-exec (network blocked for generated code and tools)`:
```bash
python3 install.py --check
```

**4. The sandbox test suite passes.** Every test should pass, and the first line should read `platform=darwin backend=sandbox-exec`:
```bash
python3 -m unittest tests.test_sandbox -v
```
It checks that, inside the sandbox:
- outbound TCP and DNS are blocked
- files can still be written in the working folder (tools need this)
- secret environment variables don't leak in
- the CPU limit stops a runaway loop
- an MCP tool call runs inside the sandbox

**5. Optional: the full suite.** On macOS the memory-limit check is skipped, because macOS doesn't enforce `RLIMIT_AS`:
```bash
python3 -m unittest discover -s tests -t .
```

If a step fails, please open an issue with the output of steps 1–4 and `sw_vers`. The most likely differences on some macOS versions are DNS (`test_dns_is_blocked`) and an extra environment variable (`test_environment_is_scrubbed`).

## Windows

Windows-specific parts: the user PATH, `.cmd` wrappers, file locking and UTF-8 I/O. Use PowerShell; run the commands from the repo folder unless noted.

**1. Prerequisites.** Each should print a version, and Python must be 3.10+:
```powershell
py -3 --version; git --version; claude --version
```
If `python` opens the Microsoft Store instead of running, turn off its alias in *Settings > Apps > Advanced app settings > App execution aliases*.

**2. Run the installer.** Run `.\install.bat`, or the `irm ... | iex` one-liner from the README. Expected:
- `[ok]` for Python, git and the Claude Code CLI
- `[warn] no code sandbox on this system: native Windows has no equivalent; WSL gives you the Linux sandbox`
- the question `Allow running code without network isolation? [y/N]`: answer **N** for this test, and expect `[warn] left disabled ...`
- `[ok] self-test: OK`, then `[ok]` for the hooks, the MCP server and the PATH

**3. Status.** Open a **new** terminal first, so the PATH change applies. Expect `hooks installed`, `MCP server `learned` registered` and `bin/ on PATH`:
```powershell
python install.py --check
```

**4. MCP server.** Expect `Scope: User config`, `Status: √ Connected` and the full path of `python.exe` as the command:
```powershell
claude mcp get learned
```

**5. PATH.** Expect a line ending in `claude-learning-harness\learned\bin` (where wrappers for scripts you approve will go), and the path of `python.exe`:
```powershell
$env:Path -split ';' | Select-String 'learned\\bin'
$env:LEARNING_HARNESS_PYTHON
```

**6. Session logging.** Start `claude` in any folder, send one message, then exit (`/exit`). Back in the repo folder, the session should appear at the top of the list:
```powershell
python review\cli.py list -n 3
```
If it doesn't, check the hook command (it should be the quoted full `python.exe` path followed by the quoted `hooks/log_session.py` path) and `store\hook_errors.log`:
```powershell
Get-Content $env:USERPROFILE\.claude\settings.json
```

**7. Tests.** All of these should pass. The network-isolation checks in `tests.test_sandbox` report as skipped, which is expected without a sandbox:
```powershell
python -m unittest tests.test_phase1 tests.test_phase2 tests.test_sandbox tests.test_install -v
```
The full suite (`python -m unittest discover -s tests -t .`) should also pass, with the Phase 3–5 and MCP tests skipped: they run generated code, which needs the Linux/macOS sandbox.

**8. Uninstall.** Expect `[ok]` lines for removing the hooks, the MCP server and the PATH entry; `LEARNING_HARNESS_PYTHON` is removed too:
```powershell
python install.py --uninstall
```

If a step fails, please open an issue with the output of steps 2–4 and 7, plus `[Environment]::OSVersion` and `py -3 --version`. The most likely trouble spots are the PATH update (step 5), how Claude Code on Windows runs hook commands (step 6), and user folders with spaces or non-ASCII characters in the name.

**Full sandbox on Windows: use WSL.** See [WSL](#wsl) below. You get the `unshare` sandbox and every test runs.

## WSL

WSL (Windows Subsystem for Linux) runs a real Linux, so this is the Linux install, plus a few WSL-specific checks. The installer detects WSL and prints `Linux (WSL2)` in its first line.

**1. WSL2, not WSL1.** In **PowerShell**, check that your distro's `VERSION` column says `2`:
```powershell
wsl -l -v
```
- If it says `1`, convert it: `wsl --set-version <distro> 2`. WSL1 has no network namespaces, so the sandbox can't work there, and the installer warns about it.
- No distro yet? `wsl --install` (installs Ubuntu; reboot when asked).

**Everything from here runs inside WSL**, in the Ubuntu (or other distro) terminal, not PowerShell.

**2. Linux tools, not the Windows ones.** WSL puts Windows programs on its PATH, so check where each one comes from:
```bash
command -v python3 git claude
```
None of the three paths may start with `/mnt/`; those are the Windows programs. If `claude` is missing or under `/mnt/c/...`, install Claude Code inside WSL, the same way as on Linux. The installer refuses a Windows-side `claude` or Python: it would register the MCP server in the Windows config with Linux paths, and that can't work. Missing tools: `sudo apt install python3 git`.

**3. The sandbox works.** This should print `0`:
```bash
unshare -rn true; echo $?
```
If it doesn't, see step 2 of [Linux](#linux): Ubuntu 24.04's AppArmor restriction can apply in WSL too.

**4. Install into the Linux filesystem.** Run the Linux one-liner from your WSL home folder:
```bash
cd ~ && curl -fsSL https://raw.githubusercontent.com/longlephucwork/claude-learning-harness/main/install.sh | bash
```
It clones to `~/claude-learning-harness`. Don't use a clone under `/mnt/c/...`: the Windows drive is slow from WSL, and file permissions misbehave there (the installer warns). Expected output:
- a first line containing `Linux (WSL2)`
- `[ok]` for Python, git and the Claude Code CLI
- `[ok] code sandbox: unshare (network blocked ...)`
- `[ok] self-test: OK`
- `[ok]` for the hooks, the MCP server and the PATH

**5. Then steps 4–9 of [Linux](#linux).** Run them inside WSL, from `~/claude-learning-harness`: `install.py --check`, `claude mcp get learned` (from `~`), the PATH, session logging, the tests (all run, nothing skipped), and uninstall.

**6. Which Claude Code sees it.** The hooks and the MCP server are installed for the Claude Code that runs **inside WSL**:
- in the WSL terminal: `claude`
- in VS Code: open the folder with the **WSL** extension (*WSL: Connect to WSL*), so Claude Code runs in WSL

A Claude Code started natively on Windows (PowerShell, or VS Code without WSL) uses the Windows config and won't log sessions or see the `learned` tools. That's expected. Use the Windows install for that, without a sandbox.

If a step fails, please open an issue with the output of steps 1–4, plus `uname -r` (run inside WSL) and `wsl --version` (run in PowerShell).

## Docker

Inside a container the installer prints `Linux (container)` in its first line. The steps work as root and as a normal user.

**Why containers need one extra flag.** Docker's default seccomp profile (its list of allowed system calls) only lets containers with `CAP_SYS_ADMIN` call `unshare`. So the code sandbox (`unshare -rn`) fails with `Operation not permitted`, and validation and MCP tools refuse to run code. `docker/seccomp-userns.json` is Docker's own default profile with **one** added rule allowing `unshare`. Every other restriction stays as Docker ships it; for example, `mount` is still refused. This is safer than the alternatives:

| Start the container with | Sandbox | What else it allows |
|---|---|---|
| (defaults) | blocked | nothing extra; the harness still logs, detects and reviews, but won't run code |
| `--security-opt seccomp=docker/seccomp-userns.json` | **works** | only the `unshare` system call (recommended) |
| `--security-opt seccomp=unconfined` | works | every system call; turns seccomp off entirely |
| `--cap-add SYS_ADMIN` | works (root only) | near-root powers over the container |

To match your Docker version exactly, regenerate the profile from Docker's published default: `python3 docker/make-seccomp-profile.py`. (Source: [moby/profiles](https://github.com/moby/profiles), Apache-2.0.)

**1. The sandbox works in your container.** Run this from the repo folder on the host. Expect `rc=1` (blocked) for the first command and `rc=0` for the second:
```bash
docker run --rm python:3.12 sh -c 'unshare -rn true; echo rc=$?'
docker run --rm --security-opt seccomp="$PWD/docker/seccomp-userns.json" python:3.12 sh -c 'unshare -rn true; echo rc=$?'
```

**2. Install and test inside a container.** Mount the repo read-only and copy it in, so the container never writes to your clone:
```bash
docker run --rm -it -v "$PWD":/src:ro --security-opt seccomp="$PWD/docker/seccomp-userns.json" python:3.12 bash
# inside the container:
mkdir ~/c && tar -C /src --exclude=./store -cf - . | tar -xf - -C ~/c && cd ~/c
python3 install.py --yes
python3 -m unittest discover -s tests -t .
```
Expected:
- `[ok] code sandbox: unshare (network blocked ...)`
- `[ok] self-test: OK`
- the suite ends with `OK`, with nothing skipped

Without the `--security-opt` flag, you get `[warn] no code sandbox ... Docker's default seccomp profile blocks unshare` and the suite still ends with `OK`, but reports the tests that run generated code as skipped. Add `--user 1000:1000 -e HOME=/tmp/h` (and create `/tmp/h`) to repeat the test as a normal user, as many dev containers run.

**3. Using it day to day in a container** (for example a dev container):
- Claude Code must be installed **inside** the container, and logged in there, since it reads its config from the container's home.
- Keep `~/.claude` and the repo's `store/` on volumes, or the logged sessions and settings vanish with the container.
- In a VS Code dev container, add the profile to `devcontainer.json`: `"runArgs": ["--security-opt", "seccomp=${localWorkspaceFolder}/docker/seccomp-userns.json"]`. It's a path on the host.
- `install.py` writes the PATH block to `~/.profile` when `$SHELL` is unset, which is common in containers. Open a login shell (`bash -l`) to pick it up.

If a step fails, please open an issue with the output of steps 1–2, plus `docker version` and the image you used.
