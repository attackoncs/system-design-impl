from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import json
import redis

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from nearby_friends.core import Error, Ring, Store, meters
from nearby_friends.backend import Backend


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [2000000000.0]
        self.path = Path(self.temp.name)/"state.sqlite"
        self.store = Store(self.path,ttl=600,retention=1000,clock=lambda:self.now[0])
        for user in ["alice","bob","eve"]:
            self.store.provision(user,user+"-private",admin=user == "alice")
            self.store.setting(user,"sharing",True)

    def tearDown(self):
        self.temp.cleanup()

    def report(self,user="alice",lat=0,lon=0,**extra):
        return self.store.update(user,{"latitude":lat,"longitude":lon,"timestamp":self.now[0],**extra})

    def friend(self):
        self.store.friendship("alice","bob","friend.request")
        self.store.friendship("bob","alice","friend.accept")

    def test_authentication_and_mutual_consent(self):
        self.assertEqual(self.store.authenticate("alice-private"),"alice")
        with self.assertRaises(Error):
            self.store.authenticate("wrong")
        with self.assertRaises(Error):
            self.store.friendship("alice","bob","friend.accept")
        self.report()
        self.report("bob")
        self.assertEqual(self.store.reports("alice")[1],[])
        self.friend()
        self.assertEqual(len(self.store.reports("alice")[1]),1)
        self.store.friendship("bob","alice","friend.remove")
        self.assertEqual(self.store.reports("alice")[1],[])

    def test_privacy_off_requires_new_location_on_reenable(self):
        self.friend()
        self.report()
        self.report("bob")
        self.store.setting("bob","sharing",False)
        self.assertEqual(self.store.reports("alice")[1],[])
        self.store.setting("bob","sharing",True)
        self.assertEqual(self.store.reports("alice")[1],[])
        with self.assertRaises(Error):
            self.report("bob")
        self.now[0] += 1
        self.report("bob")
        self.assertEqual(len(self.store.reports("alice")[1]),1)

    def test_ttl_and_clock_skew(self):
        self.report(timestamp=self.now[0]+30)
        self.now[0] += 600
        self.assertIsNone(self.store.reports("alice")[0])
        self.store.sweep()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM current").fetchone()[0],0)

    def test_report_validation_and_ordering_across_devices(self):
        for lat,lon,stamp in [(91,0,self.now[0]),(0,181,self.now[0]),(True,0,self.now[0]),(0,0,float("nan")),(0,0,self.now[0]+31),(0,0,self.now[0]-600)]:
            with self.assertRaises(Error):
                self.report(lat=lat,lon=lon,timestamp=stamp)
        self.report(device="phone")
        with self.assertRaises(Error) as caught:
            self.report(timestamp=self.now[0]-1,device="tablet")
        self.assertEqual(caught.exception.code,409)
        restarted = Store(self.path,clock=lambda:self.now[0])
        with self.assertRaises(Error):
            restarted.update("alice",{"latitude":0,"longitude":0,"timestamp":self.now[0]})

    def test_history_consent_retention_and_forgetting(self):
        self.report()
        self.assertEqual(self.store.read_history("alice"),[])
        self.store.setting("alice","history",True)
        self.now[0] += 1
        self.report()
        self.assertEqual(len(self.store.read_history("alice")),1)
        self.assertEqual(self.store.read_history("bob"),[])
        self.now[0] += 1000
        self.assertEqual(self.store.read_history("alice"),[])
        self.report()
        self.store.setting("alice","history",False)
        self.assertEqual(self.store.read_history("alice"),[])

    def test_distance_dateline_poles_and_radius_boundary(self):
        self.assertAlmostEqual(meters({"latitude":0,"longitude":179.999},{"latitude":0,"longitude":-179.999}),222.39016,places=4)
        self.assertLess(meters({"latitude":90,"longitude":0},{"latitude":90,"longitude":180}),1e-6)
        import math
        longitude = math.degrees(8046.72/6371008.8)
        self.assertAlmostEqual(meters({"latitude":0,"longitude":0},{"latitude":0,"longitude":longitude}),8046.72,places=6)

    def test_cache_corruption_outage_and_acl_revalidation(self):
        self.friend()
        self.report()
        report = self.report("bob",lon=0.001)
        cache = MagicMock()
        # Deliberate cache corruption is unit fault injection, not a fake Redis integration claim.
        cache.mget.return_value = [json.dumps({**report,"longitude":100})]
        with patch("nearby_friends.backend.redis.Redis.from_url",return_value=cache):
            backend = Backend(self.store,"redis://unused")
        self.assertLess(backend.snapshot("alice")[0]["distance_m"],120)
        cache.mget.side_effect = redis.ConnectionError("injected outage")
        cache.eval.side_effect = redis.ConnectionError("injected outage")
        self.assertEqual(len(backend.snapshot("alice")),1)
        self.store.friendship("alice","bob","friend.remove")
        self.assertEqual(backend.snapshot("alice"),[])
        backend.close()

    def test_radius_filter_and_paging_order_source(self):
        import math
        self.friend()
        self.report()
        boundary_lon = math.degrees(8046.72/6371008.8)
        self.report("bob",lon=boundary_lon)
        cache = MagicMock()
        cache.mget.return_value = [None]
        with patch("nearby_friends.backend.redis.Redis.from_url",return_value=cache):
            backend = Backend(self.store,"redis://unused")
        self.assertEqual(len(backend.snapshot("alice")),1)
        self.assertEqual(backend.snapshot("alice",radius=8046.719),[])
        with self.assertRaises(Error):
            backend.snapshot("alice",radius=float("nan"))
        backend.close()

    def test_ring_cas_permissions_stability_and_remapping(self):
        urls = {"a":"redis://127.0.0.1:1","b":"redis://127.0.0.1:2"}
        ring = Ring(urls)
        replaced = Ring({**urls,"a":"redis://127.0.0.1:3"})
        expanded = Ring({**urls,"c":"redis://127.0.0.1:4"})
        moved = 0
        for i in range(1000):
            user = str(i)
            self.assertEqual(ring.owner(user),replaced.owner(user))
            if ring.owner(user) != expanded.owner(user):
                moved += 1
                self.assertEqual(expanded.owner(user),"c")
        self.assertTrue(100 < moved < 600)
        with self.assertRaises(Error):
            self.store.set_ring("bob",urls,0)
        self.assertEqual(self.store.set_ring("alice",urls,0),{"epoch":1})
        with self.assertRaises(Error):
            self.store.set_ring("alice",urls,0)


if __name__ == "__main__":
    unittest.main()
