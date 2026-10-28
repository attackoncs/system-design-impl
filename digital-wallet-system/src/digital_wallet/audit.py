"""Cross-shard supply and participant reconciliation under writer ownership."""


class AuditMixin:
    def audit(self):
        with self.runtime.mutation():
            quorum = self.sync()
            views = [
                self.shard(i, "/internal/view")
                for i in range(len(self.config["shards"]))
            ]
            checks = [self.shard(i, "/internal/audit") for i in range(len(views))]
            total = sum(
                balance["available"] + balance["held"]
                for view in views
                for balance in view["accounts"].values()
            )
            transit = 0
            errors = []
            commands = self.state()
            for id, value in commands.items():
                source, destination = self.indices(value["intent"])
                if source == destination:
                    part = views[source]["parts"].get(id)
                    if value["decision"] is not None:
                        errors.append("decision on local transfer: " + id)
                    if part and (
                        part["intent"] != value["intent"] or part["role"] != "local"
                    ):
                        errors.append("mismatched local participant: " + id)
                    if value["status"] == "REJECTED" and (
                        not part or part["status"] != "REJECTED"
                    ):
                        errors.append("missing rejection: " + id)
                    if value["status"] == "SUCCEEDED" and (
                        not part or part["status"] != "CONFIRMED"
                    ):
                        errors.append("missing local effect: " + id)
                    continue
                for index, role in ((source, "source"), (destination, "destination")):
                    part = views[index]["parts"].get(id)
                    if part and (
                        part["intent"] != value["intent"] or part["role"] != role
                    ):
                        errors.append("mismatched cross-shard participant: " + id)
                left = views[source]["parts"].get(id, {"status": "ABSENT"})["status"]
                right = views[destination]["parts"].get(id, {"status": "ABSENT"})[
                    "status"
                ]
                if left == "CONFIRMED" and right != "CONFIRMED":
                    if value["decision"] != "COMMIT":
                        errors.append("uncommitted source confirmation: " + id)
                    transit += value["intent"]["amount"]
                if right == "CONFIRMED" and (
                    left != "CONFIRMED" or value["decision"] != "COMMIT"
                ):
                    errors.append(
                        "destination credited before source confirmation: " + id
                    )
                if value["status"] == "SUCCEEDED" and (left, right) != (
                    "CONFIRMED",
                    "CONFIRMED",
                ):
                    errors.append("missing completed effect: " + id)
                if value["status"] == "CANCELED" and (left, right) != (
                    "CANCELED",
                    "CANCELED",
                ):
                    errors.append("missing cancellation: " + id)
            for view in views:
                for id, part in view["parts"].items():
                    if (
                        id not in commands
                        or commands[id]["intent"] != part["intent"]
                        or view["shard"] not in self.indices(part["intent"])
                    ):
                        errors.append("orphan or mismatched participant: " + id)
            expected = sum(row["opening"] for row in self.config["accounts"].values())
            if total + transit != expected:
                errors.append("supply mismatch")
            if any(
                check["events"] != view["seq"] or check["head"] != view["head"]
                for view, check in zip(views, checks)
            ):
                errors.append("participant changed during audit")
            if not all(value["matches"] for value in checks):
                errors.append("event/projection mismatch")
            return {
                "matches": not errors,
                "errors": errors,
                "wallet_total": total,
                "in_transit": transit,
                "opening_supply": expected,
                "shards": checks,
                "quorum": quorum,
            }
