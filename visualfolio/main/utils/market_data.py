import pandas as pd
import os
import datetime

from django.utils import timezone


def read_historical_prices_eur_demo():
    df = pd.read_csv(os.path.join("demo", "demo_static_data", "historical_prices_eur.csv"), index_col='Gmt time', parse_dates=True)
    df.index = df.index.to_series().apply(lambda x: x.date())    
    return df


HISTORICAL_PRICES_EUR_DEMO = read_historical_prices_eur_demo()


def get_historical_prices_eur_demo_shifted():
    df = HISTORICAL_PRICES_EUR_DEMO.copy()
    last_date_in_df = pd.Timestamp(df.index.max())
    current_date = pd.Timestamp(timezone.localdate())
    delta = pd.Timedelta(days=(current_date - last_date_in_df).days)
    df.index = df.index + delta    
    return df


def _virtualize_date_back_in_time(target_date, first_date, last_date):
    if target_date > last_date:
        raise Exception(f"Only static demo market data is available. (timespan_end={target_date})")
    if target_date >= first_date:
        return target_date

    span_days = (last_date - first_date).days + 1
    days_back = (first_date - target_date).days
    mapped_offset = (-days_back) % span_days
    return first_date + datetime.timedelta(days=mapped_offset)


def get_prices_daily(instrument, quote_currency, timespan_end, timespan=datetime.timedelta(days=0)):
    if instrument == quote_currency:
        timespan_start = timespan_end - timespan

        if timespan == datetime.timedelta(days=0):
            values = pd.Series([1], index=pd.Index([timespan_end], name="date"), name=instrument)
        else:
            date_range = pd.date_range(timespan_start, timespan_end, freq="D").to_series().apply(lambda x: x.date())
            values = pd.Series(1, index=pd.Index(date_range, name="date"), name=instrument)

    else:
        data = get_historical_prices_eur_demo_shifted()

        timespan_start = timespan_end - timespan

        if quote_currency != "EUR":
            raise Exception(f"Only static demo market data is available. (quote_currency={quote_currency})")

        if instrument not in data.columns:
            raise Exception(f"Only static demo market data is available. (instrument={instrument})")

        first_date = data.index.min()
        last_date = data.index.max()

        if timespan == datetime.timedelta(days=0):
            mapped_date = _virtualize_date_back_in_time(timespan_end, first_date, last_date)
            values = pd.Series(
                [float(data.at[mapped_date, instrument])],
                index=pd.Index([timespan_end], name="date"),
                name=instrument,
            )
        else:
            date_range = pd.date_range(timespan_start, timespan_end, freq="D").to_series().apply(lambda x: x.date())
            mapped_dates = date_range.apply(lambda d: _virtualize_date_back_in_time(d, first_date, last_date))
            mapped_values = mapped_dates.apply(lambda d: float(data.at[d, instrument]))
            values = pd.Series(mapped_values.values, index=pd.Index(date_range, name="date"), name=instrument)

    return values
