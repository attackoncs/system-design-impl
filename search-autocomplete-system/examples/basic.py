"""Offline collection/building followed by cached serving and immediate removal."""
from search_autocomplete import Autocomplete, Store


def main():
    store = Store()
    try:
        frequencies = {"twitter": 35, "twitch": 29, "twilight": 25, "twin peak": 21,
                       "twitch prime": 18, "twitter search": 14}
        for query, count in frequencies.items():
            for i in range(count):
                store.record(f"{query}-{i}", query, 100)
        print("Published version:", store.build(0, 200, shards=2))
        service = Autocomplete(store)
        print("Suggestions:", service.suggest("tw"))
        store.block("twitter")
        print("After removal:", service.suggest("tw"))
    finally:
        store.close()


if __name__ == "__main__":
    main()
