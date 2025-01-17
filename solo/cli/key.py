# -*- coding: utf-8 -*-
#
# Copyright 2019 SoloKeys Developers
#
# Licensed under the Apache License, Version 2.0, <LICENSE-APACHE or
# http://apache.org/licenses/LICENSE-2.0> or the MIT license <LICENSE-MIT or
# http://opensource.org/licenses/MIT>, at your option. This file may not be
# copied, modified, or distributed except according to those terms.

import os
import sys

import click
from cryptography.hazmat.primitives import hashes
from fido2.client import ClientError as Fido2ClientError
from fido2.ctap1 import ApduError

import solo
import solo.fido2
from solo.cli.update import update


# https://pocoo-click.readthedocs.io/en/latest/commands/#nested-handling-and-contexts
@click.group()
def key():
    """Interact with Solo keys, see subcommands."""
    pass


@click.group()
def rng():
    """Access TRNG on key, see subcommands."""
    pass


@click.command()
@click.option("--count", default=8, help="How many bytes to generate (defaults to 8)")
@click.option("-s", "--serial", help="Serial number of Solo to use")
def hexbytes(count, serial):
    """Output COUNT number of random bytes, hex-encoded."""
    if not 0 <= count <= 255:
        print(f"Number of bytes must be between 0 and 255, you passed {count}")
        sys.exit(1)

    print(solo.client.find(serial).get_rng(count).hex())


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo to use")
def raw(serial):
    """Output raw entropy endlessly."""
    p = solo.client.find(serial)
    while True:
        r = p.get_rng(255)
        sys.stdout.buffer.write(r)


@click.command()
@click.option("--count", default=64, help="How many bytes to generate (defaults to 8)")
@click.option("-s", "--serial", help="Serial number of Solo to use")
def feedkernel(count, serial):
    """Feed random bytes to /dev/random."""

    if os.name != "posix":
        print("This is a Linux-specific command!")
        sys.exit(1)

    if not 0 <= count <= 255:
        print(f"Number of bytes must be between 0 and 255, you passed {count}")
        sys.exit(1)

    p = solo.client.find(serial)

    import struct
    import fcntl

    RNDADDENTROPY = 0x40085203

    entropy_info_file = "/proc/sys/kernel/random/entropy_avail"
    print(f"Entropy before: 0x{open(entropy_info_file).read().strip()}")

    r = p.get_rng(count)

    # man 4 random

    # RNDADDENTROPY
    #       Add some additional entropy to the input pool, incrementing the
    #       entropy count. This differs from writing to /dev/random or
    #       /dev/urandom, which only adds some data but does not increment the
    #       entropy count. The following structure is used:

    #           struct rand_pool_info {
    #               int    entropy_count;
    #               int    buf_size;
    #               __u32  buf[0];
    #           };

    #       Here entropy_count is the value added to (or subtracted from) the
    #       entropy count, and buf is the buffer of size buf_size which gets
    #       added to the entropy pool.

    entropy_bits_per_byte = 2  # maximum 8, tend to be pessimistic
    t = struct.pack(f"ii{count}s", count * entropy_bits_per_byte, count, r)

    with open("/dev/random", mode="wb") as fh:
        _ = fcntl.ioctl(fh, RNDADDENTROPY, t)
    print(f"Entropy after:  0x{open(entropy_info_file).read().strip()}")


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo use")
@click.option(
    "--host", help="Relying party's host", default="solokeys.dev", show_default=True
)
@click.option("--user", help="User ID", default="they", show_default=True)
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
@click.option(
    "--prompt",
    help="Prompt for user",
    default="Touch your authenticator to generate a credential...",
    show_default=True,
)
def make_credential(serial, host, user, udp, prompt):
    """Generate a credential.

    Pass `--prompt ""` to output only the `credential_id` as hex.
    """

    import solo.hmac_secret

    solo.hmac_secret.make_credential(
        host=host, user_id=user, serial=serial, output=True, prompt=prompt, udp=udp
    )


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo use")
@click.option("--host", help="Relying party's host", default="solokeys.dev")
@click.option("--user", help="User ID", default="they")
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
@click.option(
    "--prompt",
    help="Prompt for user",
    default="Touch your authenticator to generate a reponse...",
    show_default=True,
)
@click.argument("credential-id")
@click.argument("challenge")
def challenge_response(serial, host, user, prompt, credential_id, challenge, udp):
    """Uses `hmac-secret` to implement a challenge-response mechanism.

    We abuse hmac-secret, which gives us `HMAC(K, hash(challenge))`, where `K`
    is a secret tied to the `credential_id`. We hash the challenge first, since
    a 32 byte value is expected (in original usage, it's a salt).

    This means that we first need to setup a credential_id; this depends on the
    specific authenticator used. To do this, use `solo key make-credential`.

    If so desired, user and relying party can be changed from the defaults.

    The prompt can be suppressed using `--prompt ""`.
    """

    import solo.hmac_secret

    solo.hmac_secret.simple_secret(
        credential_id,
        challenge,
        host=host,
        user_id=user,
        serial=serial,
        prompt=prompt,
        output=True,
        udp=udp,
    )


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo use")
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
@click.argument("hash-type")
@click.argument("filename")
def probe(serial, udp, hash_type, filename):
    """Calculate HASH."""

    # hash_type = hash_type.upper()
    assert hash_type in ("SHA256", "SHA512", "RSA2048", "Ed25519")

    data = open(filename, "rb").read()
    # < CTAPHID_BUFFER_SIZE
    # https://fidoalliance.org/specs/fido-v2.0-id-20180227/fido-client-to-authenticator-protocol-v2.0-id-20180227.html#usb-message-and-packet-structure
    # also account for padding (see data below....)
    # so 6kb is conservative
    assert len(data) <= 6 * 1024

    p = solo.client.find(serial, udp=udp)
    import fido2

    serialized_command = fido2.cbor.dumps({"subcommand": hash_type, "data": data})
    from solo.commands import SoloBootloader

    result = p.send_data_hid(SoloBootloader.HIDCommandProbe, serialized_command)
    result_hex = result.hex()
    print(result_hex)
    if hash_type == "Ed25519":
        print(f"content: {result[64:]}")
        # print(f"content from hex: {bytes.fromhex(result_hex[128:]).decode()}")
        print(f"content from hex: {bytes.fromhex(result_hex[128:])}")
        print(f"signature: {result[:128]}")
        import nacl.signing

        # verify_key = nacl.signing.VerifyKey(bytes.fromhex("c69995185efa20bf7a88139f5920335aa3d3e7f20464345a2c095c766dfa157a"))
        verify_key = nacl.signing.VerifyKey(
            bytes.fromhex(
                "c69995185efa20bf7a88139f5920335aa3d3e7f20464345a2c095c766dfa157a"
            )
        )
        try:
            _ = verify_key.verify(result)
            verified = True
        except nacl.exceptions.BadSignatureError:
            verified = False
        print(f"verified? {verified}")
    # print(fido2.cbor.loads(result))


# @click.command()
# @click.option("-s", "--serial", help="Serial number of Solo to use")
# @click.argument("filename")
# def sha256sum(serial, filename):
#     """Calculate SHA256 hash of FILENAME."""

#     data = open(filename, 'rb').read()
#     # CTAPHID_BUFFER_SIZE
#     # https://fidoalliance.org/specs/fido-v2.0-id-20180227/fido-client-to-authenticator-protocol-v2.0-id-20180227.html#usb-message-and-packet-structure
#     assert len(data) <= 7609
#     p = solo.client.find(serial)
#     sha256sum = p.calculate_sha256(data)
#     print(sha256sum.hex().lower())

# @click.command()
# @click.option("-s", "--serial", help="Serial number of Solo to use")
# @click.argument("filename")
# def sha512sum(serial, filename):
#     """Calculate SHA512 hash of FILENAME."""

#     data = open(filename, 'rb').read()
#     # CTAPHID_BUFFER_SIZE
#     # https://fidoalliance.org/specs/fido-v2.0-id-20180227/fido-client-to-authenticator-protocol-v2.0-id-20180227.html#usb-message-and-packet-structure
#     assert len(data) <= 7609
#     p = solo.client.find(serial)
#     sha512sum = p.calculate_sha512(data)
#     print(sha512sum.hex().lower())


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo to use")
def reset(serial):
    """Reset key - wipes all credentials!!!"""
    if click.confirm(
        "Warning: Your credentials will be lost!!! Do you wish to continue?"
    ):
        print("Press the button to confirm -- again, your credentials will be lost!!!")
        solo.client.find(serial).reset()
        click.echo("....aaaand they're gone")


@click.command()
@click.option("--pin", help="PIN for to access key")
@click.option("-s", "--serial", help="Serial number of Solo to use")
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
def verify(pin, serial, udp):
    """Verify key is valid Solo Secure or Solo Hacker."""

    # Any longer and this needs to go in a submodule
    print("Please press the button on your Solo key")
    try:
        cert = solo.client.find(serial, udp=udp).make_credential(pin=pin)
    except ValueError as e:
        # python-fido2 library pre-emptively returns `ValueError('PIN required!')`
        # instead of trying, and returning  `CTAP error: 0x36 - PIN_REQUIRED`
        if "PIN required" in str(e):
            print("Your key has a PIN set. Please pass it using `--pin <your PIN>`")
            sys.exit(1)
        raise

    except Fido2ClientError as e:
        cause = str(e.cause)
        # error 0x31
        if "PIN_INVALID" in cause:
            print("Your key has a different PIN. Please try to remember it :)")
            sys.exit(1)
        # error 0x34 (power cycle helps)
        if "PIN_AUTH_BLOCKED" in cause:
            print(
                "Your key's PIN authentication is blocked due to too many incorrect attempts."
            )
            print("Please plug it out and in again, then again!")
            print(
                "Please be careful, after too many incorrect attempts, the key will fully block."
            )
            sys.exit(1)
        # error 0x32 (only reset helps)
        if "PIN_BLOCKED" in cause:
            print(
                "Your key's PIN is blocked. To use it again, you need to fully reset it."
            )
            print("You can do this using: `solo key reset`")
            sys.exit(1)
        # error 0x01
        if "INVALID_COMMAND" in cause:
            print("Error getting credential, is your key in bootloader mode?")
            print("Try: `solo program aux leave-bootloader`")
            sys.exit(1)
        raise

    solo_fingerprint = b"r\xd5\x831&\xac\xfc\xe9\xa8\xe8&`\x18\xe6AI4\xc8\xbeJ\xb8h_\x91\xb0\x99!\x13\xbb\xd42\x95"
    hacker_fingerprint = b"\xd0ml\xcb\xda}\xe5j\x16'\xc2\xa7\x89\x9c5\xa2\xa3\x16\xc8Q\xb3j\xd8\xed~\xd7\x84y\xbbx~\xf7"
    udp_fingerprint = b"\x05\x92\xe1\xb2\xba\x8ea\rb\x9a\x9b\xc0\x15\x19~J\xda\xdc16\xe0\xa0\xa1v\xd9\xb5}\x17\xa6\xb8\x0b8"

    if cert.fingerprint(hashes.SHA256()) == solo_fingerprint:
        print("Valid Solo Secure firmware from SoloKeys")
    elif cert.fingerprint(hashes.SHA256()) == hacker_fingerprint:
        print("Valid Solo Hacker firmware")
    elif cert.fingerprint(hashes.SHA256()) == udp_fingerprint:
        print("Local software key")
    else:
        print("Unknown fingerprint! ", cert.fingerprint(hashes.SHA256()))


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo to use")
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
def version(serial, udp):
    """Version of firmware on key."""

    try:
        major, minor, patch = solo.client.find(serial, udp=udp).solo_version()
        print(f"{major}.{minor}.{patch}")
    except solo.exceptions.NoSoloFoundError:
        print("No Solo found.")
        print("If you are on Linux, are your udev rules up to date?")
    except (solo.exceptions.NoSoloFoundError, ApduError):
        # Older
        print("Firmware is out of date (key does not know the SOLO_VERSION command).")


@click.command()
@click.option("-s", "--serial", help="Serial number of Solo to use")
@click.option(
    "--udp", is_flag=True, default=False, help="Communicate over UDP with software key"
)
def wink(serial, udp):
    """Send wink command to key (blinks LED a few times)."""

    solo.client.find(serial, udp=udp).wink()


key.add_command(rng)
rng.add_command(hexbytes)
rng.add_command(raw)
rng.add_command(feedkernel)
key.add_command(make_credential)
key.add_command(challenge_response)
key.add_command(reset)
key.add_command(update)
key.add_command(probe)
# key.add_command(sha256sum)
# key.add_command(sha512sum)
key.add_command(version)
key.add_command(verify)
key.add_command(wink)
