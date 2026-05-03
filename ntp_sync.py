# ntp_sync.py
"""
NTP Sync — kalibrasi jam & precision waiter.

Exports:
    ClockSync          — hasil kalibrasi
    calibrate_ntp()    — query NTP, ambil median offset (robust)
    true_time()        — UTC unix time yang sudah dikoreksi offset
    build_target_ts()  — bangun unix timestamp dari HH:MM:SS
    async_wait_until() — two-phase async waiter (coarse + spin-loop)
"""

import time
import asyncio
import datetime
import logging
import statistics
from dataclasses import dataclass

import ntplib

import config as cfg

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  DATA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClockSync:
    offset_sec    : float   # local_time + offset = true UTC
    rtt_sec       : float   # RTT terbaik (informatif saja)
    stratum       : int     # stratum NTP server
    server_ip     : str
    sample_count  : int
    offset_std_dev: float   # spread antar sample


# ─────────────────────────────────────────────────────────────────────────────
#  CALIBRATE
# ─────────────────────────────────────────────────────────────────────────────

def calibrate_ntp(
    host         : str   = cfg.NTP_HOST,
    sample_count : int   = cfg.NTP_SAMPLE_COUNT,
    timeout      : float = cfg.NTP_TIMEOUT_SEC,
) -> ClockSync:
    """
    Kirim `sample_count` query NTP, hitung offset via MEDIAN (robust).

    Kenapa median bukan best-RTT:
        RTT kecil ≠ offset paling akurat.
        Contoh: sample RTT=18ms bisa punya offset outlier karena asymmetric path.
        Median kebal terhadap outlier — selama mayoritas sample tidak bias,
        hasilnya tetap valid.

    Outlier filter:
        Sample dengan |offset - median| > 50ms dibuang sebelum median final.
        Ini menangkap kasus server NTP bermasalah atau routing aneh.

    Offset formula (RFC 5905 §8):
        offset = ((T2-T1) + (T3-T4)) / 2
    ntplib menghitung ini secara internal.
    """
    client   = ntplib.NTPClient()
    offsets  = []
    rtts     = []
    best_rtt = None   # hanya untuk info RTT di log
    best_r   = None

    log.info("Kalibrasi NTP: %s  (%d sample) …", host, sample_count)

    for i in range(sample_count):
        try:
            r = client.request(host, version=4, timeout=timeout)
            offsets.append(r.offset)
            rtts.append(r.delay)

            # Simpan sample RTT terkecil hanya untuk info stratum/server_ip
            if best_rtt is None or r.delay < best_rtt:
                best_rtt = r.delay
                best_r   = r

            log.debug("  [%d/%d] offset=%+.4f s  rtt=%.4f s  stratum=%d",
                      i + 1, sample_count, r.offset, r.delay, r.stratum)
            time.sleep(0.1)
        except Exception as exc:
            log.warning("  [%d/%d] gagal: %s", i + 1, sample_count, exc)

    if not offsets:
        raise RuntimeError("Semua query NTP gagal — periksa koneksi internet.")

    # ── Robust offset: median + outlier filter ────────────────────────────
    # Step 1: median kasar dari semua sample
    raw_median = statistics.median(offsets)

    # Step 2: buang outlier (|deviasi dari median| > 50ms)
    OUTLIER_THRESHOLD = 0.050   # 50 ms
    filtered = [o for o in offsets if abs(o - raw_median) < OUTLIER_THRESHOLD]

    if not filtered:
        # Semua dibuang (jaringan sangat tidak stabil) — fallback ke raw median
        log.warning("Semua sample masuk outlier filter — pakai raw median.")
        filtered = offsets

    final_offset = statistics.median(filtered)
    std_dev      = statistics.pstdev(offsets) if len(offsets) > 1 else 0.0

    log.debug(
        "  Offset raw: %s → filtered: %d sample → final: %+.3f ms",
        [f"{o*1000:+.1f}" for o in offsets],
        len(filtered),
        final_offset * 1000,
    )

    sync = ClockSync(
        offset_sec     = final_offset,          # ← MEDIAN, bukan best-RTT
        rtt_sec        = best_r.delay,           # informatif
        stratum        = best_r.stratum,
        server_ip      = ntplib.ref_id_to_text(best_r.ref_id),
        sample_count   = len(offsets),
        offset_std_dev = std_dev,
    )

    log.info("─" * 52)
    log.info("  NTP offset    : %+.3f ms  (median %d sample)", sync.offset_sec * 1000, len(filtered))
    log.info("  RTT terbaik   : %.3f ms",  sync.rtt_sec    * 1000)
    log.info("  Std dev       : %.3f ms",  sync.offset_std_dev * 1000)
    log.info("  Stratum       : %d",        sync.stratum)
    log.info("  Sample valid  : %d / %d",   sync.sample_count, sample_count)
    log.info("─" * 52)

    if sync.offset_std_dev > 0.010:
        log.warning("Variance tinggi (%.1f ms) — jitter jaringan signifikan.",
                    sync.offset_std_dev * 1000)
    if sync.stratum > 3:
        log.warning("Stratum %d tinggi — pertimbangkan NTP server lebih dekat.", sync.stratum)

    return sync


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def true_time(sync: ClockSync) -> float:
    """Estimasi terbaik waktu UTC Unix saat ini (dikoreksi offset NTP)."""
    return time.time() + sync.offset_sec


def build_target_ts(
    hour       : int,
    minute     : int,
    second     : int,
    microsecond: int  = 0,
    use_utc    : bool = True,
    sync       : ClockSync | None = None,
) -> float:
    """
    Bangun Unix timestamp untuk HH:MM:SS berikutnya (hari ini atau besok
    jika waktu sudah lewat). Menggunakan true_time(sync) jika sync tersedia.

    Args:
        use_utc: True = target dalam UTC (direkomendasikan — konsisten dengan NTP).
        sync: Hasil kalibrasi NTP (opsional tapi disarankan).
    """
    tz = datetime.timezone.utc if use_utc else None

    if sync:
        # Gunakan NTP time untuk menentukan 'hari ini'
        now = datetime.datetime.fromtimestamp(true_time(sync), tz)
    else:
        # Fallback ke system clock
        now = datetime.datetime.now(tz)

    target = now.replace(hour=hour, minute=minute, second=second,
                         microsecond=microsecond)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.timestamp()


# ─────────────────────────────────────────────────────────────────────────────
#  ASYNC TWO-PHASE WAITER
# ─────────────────────────────────────────────────────────────────────────────

async def async_wait_until(sync: ClockSync, target_unix: float) -> float:
    """
    Tunggu sampai true_time(sync) >= target_unix.

    Phase 1 — asyncio.sleep() dalam potongan COARSE_SLEEP_SEC
              Event loop Playwright tetap berjalan. CPU ≈ 0%.

    Phase 2 — spin-loop time.perf_counter()
              Dimulai FINE_THRESHOLD_SEC sebelum target.
              Blokir event loop maksimum 50 ms — aman karena tidak ada
              I/O Playwright yang perlu terjadi dalam jendela ini.

    Returns:
        delta_ms — selisih aktual waktu tembak vs target (ms, positif = telat).
    """
    remaining = target_unix - true_time(sync)

    if remaining < 0:
        log.warning("Target sudah lewat %.3f s yang lalu — lanjut langsung.", -remaining)
        return remaining * 1000

    target_dt = datetime.datetime.utcfromtimestamp(target_unix)
    log.info(
        "Precision wait → %s UTC  (dalam %.3f s)",
        target_dt.strftime("%H:%M:%S.%f"),
        remaining,
    )

    # ── Phase 1: asyncio.sleep ────────────────────────────────────────────
    while True:
        remaining = target_unix - true_time(sync)
        if remaining <= cfg.FINE_THRESHOLD_SEC:
            break
        await asyncio.sleep(min(cfg.COARSE_SLEEP_SEC, remaining - cfg.FINE_THRESHOLD_SEC))

    # ── Phase 2: spin-loop ────────────────────────────────────────────────
    # Anchor sekali ke NTP-corrected time, lalu andalkan perf_counter.
    # Menghindari syscall time.time() berulang di dalam spin.
    # Monotonic anchoring: eliminasi loncatan time.time() selama runtime.
    anchor_true = true_time(sync)
    anchor_perf = time.perf_counter()

    def current() -> float:
        return anchor_true + (time.perf_counter() - anchor_perf)

    while current() < target_unix:
        pass

    delta_ms = (current() - target_unix) * 1000
    sign = "+" if delta_ms >= 0 else ""
    log.info("Timer tembak. Delta: %s%.4f ms", sign, delta_ms)
    return delta_ms