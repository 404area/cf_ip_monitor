import geoip2.database, qqwry
from cf_ip_monitor.ipdata import ASN_MMDB, CITY_MMDB, COUNTRY_MMDB, QQWRY_DAT, available
print('paths_ok:', available())
r1 = geoip2.database.Reader(ASN_MMDB).asn('173.245.49.1')
print('asn:', r1.autonomous_system_number, r1.autonomous_system_organization)
r2 = geoip2.database.Reader(CITY_MMDB).city('173.245.49.1')
print('city:', r2.country.iso_code, r2.city.name)
q = qqwry.QQwry(); q.load_file(QQWRY_DAT)
print('qqwry test 173.245.49.1:', q.lookup('173.245.49.1'))