"""Версия и координаты репозитория для автообновления."""

__version__ = "0.4.3"

# Куда ходить за обновлениями. Заполняется один раз, после создания репозитория.
GITHUB_OWNER = "Qwizord"
GITHUB_REPO = "preflight"

RELEASES_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"


def as_tuple(v: str) -> tuple[int, ...]:
    """«0.4.0» и «v0.4.0» → (0, 4, 0). Нечисловые хвосты отбрасываются."""
    v = v.strip().lstrip("vV")
    out: list[int] = []
    for part in v.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def is_newer(remote: str, local: str = __version__) -> bool:
    return as_tuple(remote) > as_tuple(local)
