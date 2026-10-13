"""CFITSIO checksum statuses through fitsio, as in the T01 adapter.

fitsio's high-level verify_checksum rejects dataok=0 (absent DATASUM).
The low-level verifier reports dataok/hduok as 1 valid, 0 absent, -1 invalid.
Every HDU must have a valid HDU checksum. DATASUM may be absent.
"""
import fitsio


class ChecksumError(ValueError):
    pass


def verify(path):
    records = []
    with fitsio.FITS(str(path), mode='r') as hdus:
        for index, hdu in enumerate(hdus):
            result = hdu._FITS.verify_checksum(index + 1)
            if result['hduok'] != 1 or result['dataok'] not in (0, 1):
                raise ChecksumError(f'HDU {index}: {result}')
            records.append({'hdu': hdu.get_extname(), **result})
    return records
