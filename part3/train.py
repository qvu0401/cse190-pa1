from pathlib import Path

import pandas as pd
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score
from sklearn.pipeline import make_pipeline


DATA_DIR = Path(__file__).resolve().parent

ATTRIBUTES = [
    "gender",
    "age",
    "education",
    "household_income",
    "industry",
    "employment_status",
    "employer_size",
    "home_ownership",
    "parental_status",
]


def searches_to_text(searches):
    if not isinstance(searches, list):
        return ""

    queries = []
    for search in searches:
        if not isinstance(search, dict):
            continue

        query = search.get("query")
        if isinstance(query, str) and query.strip():
            queries.append(query.strip())

    return " ".join(queries)


def load_users(split):
    path = DATA_DIR / f"{split}.jsonl.gz"
    users = pd.read_json(
        path,
        lines=True,
        dtype=False,
        compression="infer",
    )

    users["text"] = users["searches"].map(searches_to_text)
    return users[["client_id", "text"]]


def load_labeled_users(split):
    users = load_users(split)

    labels_path = DATA_DIR / f"{split}_labels.csv"
    labels = pd.read_csv(
        labels_path,
        dtype=str,
        keep_default_na=False,
    )

    required_columns = ["client_id", *ATTRIBUTES]
    missing_columns = [
        column for column in required_columns
        if column not in labels.columns
    ]
    if missing_columns:
        raise ValueError(
            f"{labels_path.name} is missing columns: {missing_columns}"
        )

    joined = users.merge(
        labels[required_columns],
        on="client_id",
        how="left",
        validate="one_to_one",
        indicator=True,
    )

    unmatched = joined["_merge"] != "both"
    if unmatched.any() or len(labels) != len(users):
        raise ValueError(
            f"{split}: user rows and label rows did not match by client_id"
        )

    if joined[ATTRIBUTES].eq("").any().any():
        raise ValueError(f"{split}: at least one label is blank")

    return joined.drop(columns="_merge")


def make_model():
    return make_pipeline(
        TfidfVectorizer(
            ngram_range=(1, 2),
            min_df=1,
            sublinear_tf=True,
        ),
        LogisticRegression(
            C=1.0,
            max_iter=1000,
        ),
    )


def main():
    train = load_labeled_users("train")
    dev = load_labeled_users("dev")
    test = load_users("test")

    print(f"Loaded {len(train)} training users")
    print(f"Loaded {len(dev)} dev users")
    print(f"Loaded {len(test)} test users")
    print()

    print("Dev macro-F1 scores:")
    for attribute in ATTRIBUTES:
        model = make_model()
        model.fit(train["text"], train[attribute])

        dev_predictions = model.predict(dev["text"])
        score = f1_score(
            dev[attribute],
            dev_predictions,
            average="macro",
            zero_division=0,
        )
        print(f"  {attribute:20s} {score:.3f}")

    train_and_dev = pd.concat([train, dev], ignore_index=True)

    predictions = pd.DataFrame({
        "client_id": test["client_id"],
    })

    models = {}
    for attribute in ATTRIBUTES:
        model = make_model()
        model.fit(train_and_dev["text"], train_and_dev[attribute])
        models[attribute] = model
        predictions[attribute] = model.predict(test["text"])

    joblib.dump(models, DATA_DIR / "models.joblib", compress=3)
    print("Saved models to", DATA_DIR / "models.joblib")

    predictions = predictions[["client_id", *ATTRIBUTES]]
    output_path = DATA_DIR / "predictions.csv"
    predictions.to_csv(output_path, index=False)

    print()
    print(f"Wrote {len(predictions)} rows to {output_path}")


if __name__ == "__main__":
    main()
