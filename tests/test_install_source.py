"""The documented install must fetch the code that has the self-serve type."""

import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORK = "https://github.com/ball42/JAWA"


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_installer_clones_the_fork_by_default():
    installer = _read("bin", "installer.sh")
    assert f'repoUrl="{FORK}.git"' in installer
    clone = re.search(r"^\s*git clone .*$", installer, re.MULTILINE).group(0)
    assert '"$repoUrl"' in clone
    assert "jamf/JAWA" not in clone


def test_readme_install_command_uses_the_fork_develop_branch():
    readme = _read("README.md")
    command = re.search(
        r"^\s*curl -O (\S+installer\.sh) && sudo bash \./installer\.sh(.*)$",
        readme,
        re.MULTILINE,
    )
    assert command, "README has no installer command"
    url, args = command.group(1), command.group(2)
    assert url == (
        "https://raw.githubusercontent.com/ball42/JAWA/develop/"
        "bin/installer.sh"
    )
    # The installer defaults to main, which has no self-serve type.
    assert args.split() == ["branch", "develop"]
