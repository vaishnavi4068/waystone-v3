from waystone3.workspace.passwords import generate_password, hash_password, verify_password


def test_hash_roundtrip_and_tamper() -> None:
    stored = hash_password("unique-pass-1")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("unique-pass-1", stored)
    assert not verify_password("unique-pass-2", stored)
    assert not verify_password("unique-pass-1", stored + "0")
    assert not verify_password("unique-pass-1", "")


def test_generated_passwords_are_unique() -> None:
    issued = {generate_password() for _ in range(20)}
    assert len(issued) == 20
    assert all(len(p) >= 8 for p in issued)
