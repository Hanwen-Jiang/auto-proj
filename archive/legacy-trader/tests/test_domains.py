from pathlib import Path

import pytest

from a6api_auto.domains import load_upstream_domains
from a6api_auto.errors import A6AutoError


def test_bundled_domain_catalog_has_30_unique_https_origins() -> None:
    domains = load_upstream_domains()

    assert len(domains) == 30
    assert len(set(domains)) == 30
    assert all(value.startswith("https://") for value in domains)
    assert "https://api.hsmiai.xyz" in domains
    assert "https://xgu5wrpu95qdb.mashaji.xyz" in domains


def test_external_domain_file_normalizes_and_rejects_paths(tmp_path: Path) -> None:
    path = tmp_path / "domains.txt"
    path.write_text("one.test\nhttps://two.test/\none.test\n")
    assert load_upstream_domains(path) == ("https://one.test", "https://two.test")

    path.write_text("https://one.test/v1\n")
    with pytest.raises(A6AutoError, match="upstream_domain_invalid"):
        load_upstream_domains(path)
