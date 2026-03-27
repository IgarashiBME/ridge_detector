#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
UBX protocol message building extracted from serial_ridge_detector_zed.py.
Provides ubx_checksum and build_ubx_nav_relposned.
"""

import struct

UBX_HEADER1 = 0xB5
UBX_HEADER2 = 0x62

UBX_CLASS_NAV = 0x01
UBX_ID_RELPOSNED = 0x3C
UBX_PAYLOAD_LEN = 64


def ubx_checksum(data: bytes) -> bytes:
    """UBX checksum over: CLASS, ID, LENGTH(2), PAYLOAD.
    Returns CK_A, CK_B.
    """
    ck_a = 0
    ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def build_ubx_nav_relposned(
    relPosN_cm: int,
    relPosE_cm: int,
    gnssFixOK: int,
    carrSoln: int,
    refStationId: int = 0,
    iTOW_ms: int = 0,
    relPosD_cm: int = 0,
    relPosValid: int = 1,
) -> bytes:
    """Build UBX-NAV-RELPOSNED (0x01 0x3C) message.

    relPosN/E/D are I4 in cm.
    flags:
      bit0 gnssFixOK
      bit2 relPosValid
      bits4..3 carrSoln (0,1,2)
    """
    if not (0 <= refStationId <= 4095):
        raise ValueError("refStationId must be in 0..4095")
    if not (0 <= iTOW_ms <= 0xFFFFFFFF):
        raise ValueError("iTOW must fit in U4")
    if carrSoln not in (0, 1, 2):
        raise ValueError("carrSoln must be 0, 1, or 2")
    if gnssFixOK not in (0, 1):
        raise ValueError("gnssFixOK must be 0 or 1")
    if relPosValid not in (0, 1):
        raise ValueError("relPosValid must be 0 or 1")

    version = 0x01
    reserved0 = 0x00

    relPosLength_cm = 0
    relPosHeading_1e5deg = 0

    reserved1 = bytes(4)

    relPosHPN = 0
    relPosHPE = 0
    relPosHPD = 0
    relPosHPLength = 0

    accN = 0
    accE = 0
    accD = 0
    accLength = 0
    accHeading = 0

    reserved2 = bytes(4)

    flags = 0
    flags |= (gnssFixOK & 0x1) << 0
    flags |= (relPosValid & 0x1) << 2
    flags |= (carrSoln & 0x3) << 3

    payload = struct.pack(
        "<BBH I i i i i i 4s b b b b I I I I I 4s I",
        version,
        reserved0,
        refStationId,
        iTOW_ms,
        relPosN_cm,
        relPosE_cm,
        relPosD_cm,
        relPosLength_cm,
        relPosHeading_1e5deg,
        reserved1,
        relPosHPN,
        relPosHPE,
        relPosHPD,
        relPosHPLength,
        accN,
        accE,
        accD,
        accLength,
        accHeading,
        reserved2,
        flags,
    )

    if len(payload) != UBX_PAYLOAD_LEN:
        raise RuntimeError(f"payload length mismatch: {len(payload)} != {UBX_PAYLOAD_LEN}")

    header_wo_sync = struct.pack("<BBH", UBX_CLASS_NAV, UBX_ID_RELPOSNED, UBX_PAYLOAD_LEN)
    chk = ubx_checksum(header_wo_sync + payload)

    msg = bytes([UBX_HEADER1, UBX_HEADER2]) + header_wo_sync + payload + chk
    return msg
