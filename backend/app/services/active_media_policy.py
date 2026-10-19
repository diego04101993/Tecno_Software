"""Deterministic filename policy; no MIME registry, filesystem or byte decoding."""
from enum import Enum


class ActiveMediaPolicy(str, Enum):
    HTML_INERT = "html_inert"
    SVG_RESTRICTED = "svg_restricted"
    NORMAL_MEDIA = "normal_media"


_HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".xht")
_SVG_SUFFIXES = (".svg", ".svgz")
_ENCODING_SUFFIXES = (".gz", ".z", ".bz2", ".xz", ".br")


def classify_active_media(filename: str) -> ActiveMediaPolicy:
    """Classify a validated basename, allowing exactly one outer encoding suffix.

    Upload callers normalize/validate names first; delivery callers use the
    basename of the safely resolved physical file. Historical names stay literal.
    """
    # Inspect terminal text, including dotfiles, without path suffix semantics.
    candidate = filename.casefold()
    for encoding in _ENCODING_SUFFIXES:
        if candidate.endswith(encoding):
            candidate = candidate[:-len(encoding)]
            break
    if candidate.endswith(_HTML_SUFFIXES):
        return ActiveMediaPolicy.HTML_INERT
    if candidate.endswith(_SVG_SUFFIXES):
        return ActiveMediaPolicy.SVG_RESTRICTED
    return ActiveMediaPolicy.NORMAL_MEDIA
