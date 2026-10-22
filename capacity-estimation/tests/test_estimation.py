from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from capacity_estimation.model import availability,estimate,parse_bytes,sweep


class EstimationTests(unittest.TestCase):
    def test_book_media_example_exact_decimal_units(self):
        result=estimate(json.loads((ROOT/"examples/social-media.json").read_text()))
        values=result["estimates"]
        self.assertEqual(values["daily_active_users"],"150000000")
        self.assertEqual(values["writes_per_day"],"300000000")
        self.assertEqual(values["logical_bytes_per_day"],"30000000000000")
        self.assertEqual(values["retained_logical_bytes"],"54750000000000000")
        self.assertLess(abs(Decimal(values["average_write_qps"])-Decimal(300000000)/86400),Decimal("1e-20"))

    def test_binary_decimal_units_and_fractional_bytes(self):
        self.assertEqual(parse_bytes("1 MB"),1_000_000)
        self.assertEqual(parse_bytes("1 MiB"),1_048_576)
        self.assertEqual(parse_bytes("0.5 KiB"),512)
        for value in ("1 Mb","1 mb","1","1XB","0.1 B","-1 B",True):
            with self.assertRaises(ValueError):
                parse_bytes(value)

    def test_storage_replication_compression_index_order(self):
        result=estimate({"monthly_active_users":10,"daily_active_ratio":1,"writes_per_dau":1,"metadata_size":"100 B","media_fraction":0,
                         "retention_days":2,"replicas":3,"compression_ratio":"0.5","index_overhead_ratio":"0.2","cache_fraction":"0.1","cache_item_size":"10 B"})["estimates"]
        self.assertEqual(result["retained_logical_bytes"],"2000")
        self.assertEqual(result["physical_bytes_with_replicas_and_indexes"],"3600")
        self.assertEqual(result["cache_bytes"],"20")

    def test_network_bits_and_littles_law(self):
        values=estimate({"monthly_active_users":86400,"daily_active_ratio":1,"writes_per_dau":1,"reads_per_dau":2,
                         "metadata_size":"100 B","media_fraction":0,"read_response_size":"50 B","peak_factor":5,"mean_latency_ms":200})["estimates"]
        self.assertEqual(values["average_ingress_bits_per_second"],"800")
        self.assertEqual(values["average_egress_bits_per_second"],"800")
        self.assertEqual(values["peak_total_qps"],"15")
        self.assertEqual(values["estimated_peak_inflight_at_mean_latency"],"3")

    def test_server_ceiling_utilization_headroom_and_spares(self):
        result=estimate({"monthly_active_users":86400,"daily_active_ratio":1,"writes_per_dau":1,"reads_per_dau":0,"peak_factor":1,
                         "server_qps":2,"utilization":"0.5","safety_factor":"1.01","spare_nodes":1})
        self.assertEqual(result["nodes"],{"serving":2,"spare":1,"total":3})

    def test_exact_availability_budgets_and_boundaries(self):
        values=availability("99.99")["downtime_seconds"]
        self.assertEqual(values["day"],"8.64")
        self.assertEqual(values["month_30_days"],"259.2")
        self.assertEqual(values["year_365_days"],"3153.6")
        self.assertEqual(availability(100)["downtime_seconds"]["day"],"0")
        self.assertEqual(availability(0)["downtime_seconds"]["day"],"86400")

    def test_invalid_assumptions_and_zero_traffic(self):
        for fields in ({"replicas":0},{"replicas":"1.5"},{"utilization":0},{"media_fraction":"1.1"},{"daily_active_ratio":True},
                       {"daily_active_ratio":0.5},{"monthly_active_users":"NaN"},{"unknown":1},{"peak_factor":"1e999"},{"availability_percent":101},{"writes_per_dau":"0e-100000000"}):
            with self.assertRaises(ValueError):
                estimate(fields)
        result=estimate({"monthly_active_users":0})
        self.assertEqual(result["nodes"]["serving"],0)
        self.assertEqual(result["estimates"]["physical_bytes_with_replicas_and_indexes"],"0")

    def test_sensitivity_isolated_scenarios_and_monotonic_growth(self):
        inputs={"peak_factor":2}
        results=sweep(inputs,"peak_factor",[2,4,8])
        peaks=[Decimal(row["result"]["estimates"]["peak_total_qps"]) for row in results]
        self.assertLess(abs(peaks[1]-peaks[0]*2),Decimal("1e-20"))
        self.assertLess(abs(peaks[2]-peaks[0]*4),Decimal("1e-20"))
        self.assertEqual(inputs,{"peak_factor":2})
        with self.assertRaises(ValueError):
            sweep({},"__dict__",[1])

    def test_cli_real_json_units_and_sweep(self):
        command=[sys.executable,"-m","capacity_estimation.cli"]
        env={**os.environ,"PYTHONPATH":str(ROOT/"src")}
        units=json.loads(subprocess.check_output([*command,"units","1 MiB"],cwd=ROOT,text=True,env=env))
        self.assertEqual(units["bytes"],"1048576")
        result=json.loads(subprocess.check_output([*command,"estimate","--scenario","examples/social-media.json","--sweep","peak_factor=2,4"],cwd=ROOT,text=True,env=env))
        self.assertEqual(len(result),2)


if __name__=="__main__":
    unittest.main()
