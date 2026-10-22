from decimal import Decimal,InvalidOperation,ROUND_CEILING,localcontext
import re


UNITS={"B":1,**{name+"B":1000**index for index,name in enumerate(("K","M","G","T","P"),1)},**{name+"iB":1024**index for index,name in enumerate(("K","M","G","T","P"),1)}}


def number(value,low=0,high="1e30",low_open=False):
    if isinstance(value,bool) or isinstance(value,float) or not isinstance(value,(str,int,Decimal)):
        raise ValueError("use a decimal string or integer, never a binary float/bool")
    if isinstance(value,str) and (len(value)>100 or not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",value)):
        raise ValueError("invalid bounded decimal input")
    try:
        result=Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not result.is_finite() or result<Decimal(str(low)) or result>Decimal(str(high)) or (low_open and result==Decimal(str(low))):
        raise ValueError("decimal outside allowed range")
    if not -30<=result.as_tuple().exponent<=30:
        raise ValueError("decimal exponent exceeds reference precision limits")
    return result


def text(value):
    return format(value,"f").rstrip("0").rstrip(".") if "." in format(value,"f") else format(value,"f")


def parse_bytes(value):
    if not isinstance(value,str) or len(value)>120:
        raise ValueError("byte quantity must be a string with explicit units")
    match=re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s+(B|KB|MB|GB|TB|PB|KiB|MiB|GiB|TiB|PiB)\s*",value)
    if not match:
        raise ValueError("use e.g. '1 MB' (decimal) or '1 MiB' (binary); lowercase b is bits")
    with localcontext() as context:
        context.prec=80
        result=number(match[1])*UNITS[match[2]]
        if result!=result.to_integral_value():
            raise ValueError("a byte quantity must resolve to whole bytes")
        return result


def availability(percent):
    with localcontext() as context:
        context.prec=80
        value=number(percent,0,100)
        periods={"day":86400,"week":7*86400,"month_30_days":30*86400,"year_365_days":365*86400}
        return {"availability_percent":text(value),"downtime_seconds":{name:text(Decimal(seconds)*(1-value/100)) for name,seconds in periods.items()},"assumption":"30-day month; 365-day year; no SLA exclusions"}


DEFAULTS={"monthly_active_users":300_000_000,"daily_active_ratio":"0.5","writes_per_dau":2,"reads_per_dau":0,"peak_factor":2,
          "metadata_size":"0 B","media_fraction":"0.1","media_size":"1 MB","read_response_size":"0 B","retention_days":1825,
          "replicas":1,"compression_ratio":1,"index_overhead_ratio":0,"cache_fraction":0,"cache_item_size":"0 B",
          "server_qps":1000,"utilization":"0.7","safety_factor":1,"spare_nodes":0,"mean_latency_ms":100,"availability_percent":"99.99"}


def estimate(inputs):
    if not isinstance(inputs,dict) or set(inputs)-set(DEFAULTS):
        raise ValueError("unknown scenario fields")
    with localcontext() as context:
        context.prec=80
        values={**DEFAULTS,**inputs}
        n={key:number(value) for key,value in values.items() if not key.endswith("size")}
        for key in ("daily_active_ratio","media_fraction","cache_fraction"):
            number(n[key],0,1)
        for key in ("compression_ratio","utilization"):
            number(n[key],0,1,low_open=True)
        for key in ("peak_factor","safety_factor"):
            number(n[key],1)
        number(n["server_qps"],0,low_open=True)
        number(n["availability_percent"],0,100)
        for key in ("monthly_active_users","retention_days","replicas","spare_nodes"):
            if n[key]!=n[key].to_integral_value() or (key=="replicas" and n[key]<1):
                raise ValueError(key+" must be a whole count, replicas >= 1")
        sizes={key:parse_bytes(value) for key,value in values.items() if key.endswith("size")}
        dau=n["monthly_active_users"]*n["daily_active_ratio"]
        writes=dau*n["writes_per_dau"]
        reads=dau*n["reads_per_dau"]
        average_record=sizes["metadata_size"]+n["media_fraction"]*sizes["media_size"]
        daily_bytes=writes*average_record
        retained_bytes=daily_bytes*n["retention_days"]
        compressed=retained_bytes*n["compression_ratio"]
        physical=compressed*n["replicas"]*(1+n["index_overhead_ratio"])
        peak_qps=(writes+reads)/86400*n["peak_factor"]
        baseline_nodes=(peak_qps*n["safety_factor"]/(n["server_qps"]*n["utilization"])).to_integral_value(rounding=ROUND_CEILING)
        ingress=daily_bytes*8/86400
        egress=reads*sizes["read_response_size"]*8/86400
        quantities={"daily_active_users":dau,"writes_per_day":writes,"reads_per_day":reads,"average_write_qps":writes/86400,"average_read_qps":reads/86400,
                    "peak_write_qps":writes/86400*n["peak_factor"],"peak_read_qps":reads/86400*n["peak_factor"],"peak_total_qps":peak_qps,
                    "average_record_bytes":average_record,"logical_bytes_per_day":daily_bytes,"retained_logical_bytes":retained_bytes,"compressed_primary_bytes":compressed,
                    "physical_bytes_with_replicas_and_indexes":physical,"cache_bytes":writes*n["retention_days"]*n["cache_fraction"]*sizes["cache_item_size"],
                    "average_ingress_bits_per_second":ingress,"peak_ingress_bits_per_second":ingress*n["peak_factor"],"average_egress_bits_per_second":egress,"peak_egress_bits_per_second":egress*n["peak_factor"],
                    "estimated_peak_inflight_at_mean_latency":peak_qps*n["mean_latency_ms"]/1000}
        return {"assumptions":{key:text(value) if isinstance(value,(Decimal,int)) and not isinstance(value,bool) else value for key,value in values.items()},
                "estimates":{key:text(value) for key,value in quantities.items()},
                "nodes":{"serving":int(baseline_nodes),"spare":int(n["spare_nodes"]),"total":int(baseline_nodes+n["spare_nodes"])},
                "availability":availability(n["availability_percent"]),
                "notes":["Estimates, not measurements or a capacity/SLA guarantee.","MB=10^6 bytes; MiB=2^20 bytes. Rates use bits/s, 8 bits per byte.",
                         "Storage compression applies before replica and index multipliers; index overhead is on each compressed replica.",
                         "Cache holds a fraction of retained write records at explicit cache-item size; no cache replication multiplier.",
                         "Network uses uncompressed average write/read payloads, excluding protocol, replication and cache traffic.",
                         "Node count assumes a supplied mixed-workload QPS benchmark; spare nodes are explicit, not an availability proof.",
                         "Inflight estimate applies Little's law to assumed peak arrival rate and mean latency, not p99."]}


def sweep(inputs,field,values):
    if field not in ("monthly_active_users","peak_factor","media_fraction","replicas","utilization","safety_factor") or not 1<=len(values)<=20:
        raise ValueError("unsupported or oversized sensitivity sweep")
    return [{"field":field,"value":str(value),"result":estimate({**inputs,field:value})} for value in values]
