import os
import json
import datetime
import re
import zoneinfo

import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.shortcuts import render
from django.views import View
from django.contrib.auth import login, logout
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.contrib import messages
from django.db import transaction
from django.db import IntegrityError
from django.views.generic import TemplateView, ListView
from django.db.models import Count
from django.urls import reverse
from django.http import Http404, JsonResponse
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import F

from django.conf import settings

from main.utils.market_data import get_prices_daily
from main.services.stateless.visualization import (
    hex_to_hsl_components,
    generate_shades,
    humanize_number,
    opacity,
)
from main.services.stateless.calculation import BalanceCalculationService
from main.services.stateless.transaction_service import TransactionService

from .models import Transaction, Account, Asset, BalanceHistory, AssetClass, Requisition, AccountAsset
from .constants import COUNTRY_CHOICES


class CustomErrorView(View):
    def get(self, request, *args, **kwargs):
        return redirect("overview")

    def post(self, request, *args, **kwargs):
        return redirect("overview")


class LoginView(View):
    def dispatch(self, request, *args, **kwargs):
        if self.request.user.is_authenticated:
            return redirect("overview")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        # Render the login form with pre-filled credentials
        return render(
            request,
            "main/login.html",
        )

    def post(self, request, *args, **kwargs):
        User = get_user_model()

        # Get credentials from POST data
        username_post = request.POST.get("username")
        password_post = request.POST.get("password")

        try:
            # Get user by username
            user = User.objects.get(username=username_post)
            
            # Check password
            if user.check_password(password_post):
                # Log the user in
                login(request, user)
                return redirect("overview")
            else:
                messages.error(request, "Invalid credentials")
        except User.DoesNotExist:
            messages.error(request, "Invalid credentials")
        
        return render(request, "main/login.html")


class RegistrationView(View):
    def dispatch(self, request, *args, **kwargs):
        if self.request.user.is_authenticated:
            return redirect("overview")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return render(request, "main/register.html")

    def post(self, request, *args, **kwargs):
        User = get_user_model()
        username = request.POST.get("username")
        first_name = request.POST.get("first_name")
        last_name = request.POST.get("last_name")
        password = request.POST.get("password")
        password_confirm = request.POST.get("password_confirm")

        if not all([username, first_name, last_name, password, password_confirm]):
            messages.error(request, "All fields are required.")
            return render(request, "main/register.html", {"username": username, "first_name": first_name, "last_name": last_name})

        if password != password_confirm:
            messages.error(request, "Passwords do not match.")
            return render(request, "main/register.html", {"username": username, "first_name": first_name, "last_name": last_name})
        
        if User.objects.filter(username=username).exists():
            messages.error(request, "Username already exists.")
            return render(request, "main/register.html", {"first_name": first_name, "last_name": last_name})

        try:
            user = User.objects.create_user(username=username, password=password, first_name=first_name, last_name=last_name)
            user.save()
            login(request, user)
            return redirect("overview")
        except Exception as e:
            messages.error(request, f"An error occurred: {e}")
            return render(request, "main/register.html", {"username": username, "first_name": first_name, "last_name": last_name})


class LogoutView(View):
    def get(self, request, *args, **kwargs):
        # Drain all pending messages so they don't leak to the login page
        storage = messages.get_messages(request)
        list(storage)
        logout(request)
        return redirect("login")


class OverviewView(LoginRequiredMixin, TemplateView):
    template_name = "main/overview.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get valid range dates
        valid_range_start, valid_range_end = BalanceHistory.get_valid_range_dates(self.request.user)
        
        # Check if we have enough data points
        has_enough_data = (
            valid_range_start is not None 
            and valid_range_end is not None 
            and valid_range_start != valid_range_end
        )

        if not has_enough_data:
            context["has_enough_data"] = False
            context["total_asset_value"] = 0
            context["base_currency"] = settings.BASE_CURRENCY["symbol"]
            context["graph_data"] = "{}"
            return context

        # Get latest balances
        latest_balances = (
            BalanceHistory.get_latest_valid_balances(self.request.user)
            .select_related("asset__asset_class")
            .values("account", "asset", "balance")
        )
        balances_df = pd.DataFrame(list(latest_balances))

        # Calculate current values
        today = timezone.localdate()
        balances_df["current_price"] = balances_df.apply(
            lambda x: get_prices_daily(
                x["asset"],
                settings.BASE_CURRENCY["code"],
                today,
            ),
            axis=1,
        )
        balances_df["current_value"] = (
                balances_df["current_price"] * balances_df["balance"]
        )

        # Calculate totals
        total_asset_value = balances_df["current_value"].sum()

        # Get users BalanceHistories
        balance_histories = BalanceHistory.get_valid_range_records(
            self.request.user
        ).values(
            "account__name",
            "asset",
            "asset__asset_class__name",
            "asset__asset_class__color",
            "balance",
            "date",
        )
        balance_histories_df = pd.DataFrame(list(balance_histories))
        balance_histories_df.rename(
            columns={
                "account__name": "account",
                "asset": "asset",
                "asset__asset_class__name": "asset_class",
                "asset__asset_class__color": "asset_class_color",
            },
            inplace=True,
        )

        balance_histories_df["account_asset"] = (
                balance_histories_df["account"] + " - " + balance_histories_df["asset"]
        )

        # Get user's Transactions
        transactions = Transaction.objects.filter(account__user=self.request.user)

        # Calculate value history from BalanceHistory
        timespan_end = balance_histories_df["date"].max()
        timespan_start = balance_histories_df["date"].min()
        timespan = timespan_end - timespan_start

        # Collect prices by asset
        price_dfs = []
        for asset in balance_histories_df['asset'].unique():
            asset_daily_prices = get_prices_daily(
                instrument=asset,
                quote_currency=settings.BASE_CURRENCY["code"],
                timespan_end=timespan_end,
                timespan=timespan
            )
            temp_df = asset_daily_prices.reset_index().rename(columns={asset: 'price'})
            temp_df['asset'] = asset
            price_dfs.append(temp_df)

        # Combine all price dfs
        combined_prices_df = pd.concat(price_dfs, ignore_index=True)

        # Merge
        balance_histories_df = balance_histories_df.merge(combined_prices_df, on=['date', 'asset'], how='left')

        balance_histories_df["value"] = (
                balance_histories_df["price"] * balance_histories_df["balance"]
        )

        pivot_total_value = (
            balance_histories_df.groupby("date", as_index=False)["value"]
            .sum()
            .reset_index()
        )

        # Calculate relative balance history accounting for Transactions only
        transactions_by_date = {}
        for txn in transactions:
            transaction_date = txn.datetime.date()
            transactions_by_date.setdefault(transaction_date, []).append(txn)

        # Generate date range from balance history timespan (not transaction dates)
        data = list()
        value = 0
        
        # Use the balance history date range, not transaction date range
        for current_date in sorted(balance_histories_df['date'].unique()):
            if current_date in transactions_by_date:
                unique_assets = set(
                    [t.asset for t in transactions_by_date[current_date]]
                )
                assets_price_mapping = {}
                for a in unique_assets:
                    assets_price_mapping[a] = get_prices_daily(
                        a.code, settings.BASE_CURRENCY["code"], current_date
                    )

                for txn in transactions_by_date[current_date]:
                    asset_price = assets_price_mapping[txn.asset].iloc[0]
                    transaction_value = asset_price * txn.amount
                    value += transaction_value

            data.append({"date": current_date, "rel_value_transactions_only": value})

        relative_balance_history_transactions_only = pd.DataFrame(data)

        # Group values by asset class
        grouping_field = "asset_class"
        grouped = balance_histories_df.groupby(
            ["date", grouping_field], as_index=False
        ).agg({"value": "sum"})
        pivot = grouped.pivot(
            index="date", columns=grouping_field, values="value"
        ).reset_index()

        # Generate themed color map for stramgraph
        color_map = (
            balance_histories_df[["asset_class", "asset_class_color"]]
            .drop_duplicates()
            .set_index("asset_class", drop=True)
        )
        color_map["hsl_dark_background"] = color_map["asset_class_color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 75%, 45%)"
        )
        color_map["hsl_light_background"] = color_map["asset_class_color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 96%, 66%)"
        )

        groups = list(filter(lambda x: x != "date", pivot.columns))

        # Set transacitonal asset (or asset class) as first to improve chart readability
        if grouping_field == "asset":
            first_group = settings.BASE_CURRENCY["code"]
            other_groups = list(filter(lambda x: x != first_group, groups))
            groups = [first_group] + other_groups

        elif grouping_field == "asset_class":
            fiat_label = "Fiat Currencies"
            has_fiat = fiat_label in groups

            if has_fiat:
                other_groups = [g for g in groups if g != fiat_label]
            else:
                other_groups = list(groups)

            # Sort the remaining asset classes by ascending volatility to improve plot readability
            volatility_mapping = {
                asset_class.name: asset_class.volatility_index
                for asset_class in AssetClass.objects.filter(name__in=other_groups)
            }

            other_groups = sorted(
                other_groups,
                key=lambda group: volatility_mapping.get(group)
            )

            groups = [fiat_label] + other_groups if has_fiat else other_groups
            first_group = groups[0]

        else:
            first_group = groups[0]
            pass

        # Calculate relative balance history accounting for investment P/L only
        # by subtracting transaction balance history from total balance history
        df = pivot_total_value.merge(
            relative_balance_history_transactions_only, on="date"
        ).merge(pivot, on="date")
        df["rel_value_capital_gain_only"] = (
                df["value"] - df["rel_value_transactions_only"]
        )

        # Invert relative balance history accounting for investment P/L only
        df["inverted_rel_value_capital_gain_only"] = -df["rel_value_capital_gain_only"]

        # Adjust y so the thickness at each x matches the tot user value at that time
        y0 = df.at[0, "inverted_rel_value_capital_gain_only"]
        tot_user_value_0 = df.at[0, "value"]
        target_y0 = df.at[0, "rel_value_transactions_only"] - tot_user_value_0
        delta_y0 = target_y0 - y0
        df["inverted_rel_value_capital_gain_only"] += delta_y0

        # Calculate internal boundaries of streamgraph
        for i in range(len(groups[:-1])):
            if i == 0:
                previous_boundary = "rel_value_transactions_only"
            else:
                previous_boundary = f"{groups[i - 1]}_{groups[i]}_internal_boundary"

            col = f"{groups[i]}_{groups[i + 1]}_internal_boundary"
            df[col] = df[previous_boundary] - df[groups[i]]

        last_group_name = groups[-1]

        # Prepare transactions and trades data grouped by date
        transactions_data_by_date = {}
        all_transactions = Transaction.objects_extended.filter(
            account__user=self.request.user,
            datetime__date__in=df["date"].tolist()
        ).select_related("account", "asset", "paired_transaction", "paired_transaction__asset")
        
        # Track seen trade IDs to avoid duplicates
        seen_trade_ids = set()
        
        for txn in all_transactions:
            txn_date = txn.datetime.date().isoformat()
            
            if txn_date not in transactions_data_by_date:
                transactions_data_by_date[txn_date] = {
                    "transactions": [],
                    "trades": []
                }
            
            if txn.is_trade_side:
                # Handle trades
                if txn.id in seen_trade_ids:
                    continue
                    
                paired = txn.paired_transaction
                if paired:
                    seen_trade_ids.add(txn.id)
                    seen_trade_ids.add(paired.id)
                    
                    # Determine which is "from" and which is "to"
                    if txn.amount < 0:
                        from_txn, to_txn = txn, paired
                    else:
                        from_txn, to_txn = paired, txn
                    
                    # Get prices for both assets
                    from_price = get_prices_daily(
                        from_txn.asset.code,
                        settings.BASE_CURRENCY["code"],
                        txn.datetime.date()
                    ).iloc[0]
                    
                    to_price = get_prices_daily(
                        to_txn.asset.code,
                        settings.BASE_CURRENCY["code"],
                        txn.datetime.date()
                    ).iloc[0]
                    
                    # Calculate absolute values
                    from_value = abs(from_txn.amount * from_price)
                    to_value = abs(to_txn.amount * to_price)
                    
                    transactions_data_by_date[txn_date]["trades"].append({
                        "from_asset": from_txn.asset.code,
                        "to_asset": to_txn.asset.code,
                        "from_amount": abs(from_txn.amount),
                        "to_amount": abs(to_txn.amount),
                        "from_value": from_value,
                        "to_value": to_value,
                        "datetime": txn.datetime.isoformat(),
                    })
            else:
                # Handle regular transactions
                price = get_prices_daily(
                    txn.asset.code,
                    settings.BASE_CURRENCY["code"],
                    txn.datetime.date()
                ).iloc[0]
                
                value = txn.amount * price
                
                transactions_data_by_date[txn_date]["transactions"].append({
                    "entity": txn.entity,
                    "amount": txn.amount,
                    "asset": txn.asset.code,
                    "value": value,
                    "category": txn.category or "",
                    "datetime": txn.datetime.isoformat(),
                })

        # Serialize streamgraph data for client-side D3 rendering
        theme = self.request.theme
        opacity_val = opacity[theme]
        boundary_cols = [c for c in df.columns if c.endswith("_internal_boundary")]
        boundaries = (
            ["rel_value_transactions_only"]
            + boundary_cols
            + ["inverted_rel_value_capital_gain_only"]
        )

        layers = []
        for i, group_name in enumerate(groups):
            upper_key = boundaries[i]
            lower_key = boundaries[i + 1]
            color = color_map.at[group_name, f"hsl_{theme}_background"]
            color = color.replace("hsl(", "hsla(").replace(")", f",{opacity_val})")
            layers.append({
                "name": group_name,
                "upper": df[upper_key].tolist(),
                "lower": df[lower_key].tolist(),
                "color": color,
            })

        # Compute y-axis gridlines and ruler metadata
        y_data_min = df["inverted_rel_value_capital_gain_only"].min()
        y_data_max = df["rel_value_transactions_only"].max()
        y_data_range = y_data_max - y_data_min
        y_min = y_data_min - (y_data_range * 0.25)
        y_max = y_data_max + (y_data_range * 0.15)

        nice_intervals = [x * 10**i for i in range(0, 10) for x in (1, 2, 5)]
        target_interval = (y_max - y_min) / 10
        chosen_interval = min(nice_intervals, key=lambda x: abs(x - target_interval))
        num_lines = int((y_max - y_min) // chosen_interval)
        if num_lines % 2 == 1:
            num_lines += 1
        y_axis_lines = [y_min + i * chosen_interval for i in range(num_lines + 1)]

        graph_data = json.dumps({
            "dates": [d.isoformat() for d in df["date"]],
            "layers": layers,
            "values": df["value"].tolist(),
            "transactionsByDate": transactions_data_by_date,
            "yMin": y_min,
            "yMax": y_min + chosen_interval * num_lines * 1.01,
            "yAxisLines": y_axis_lines,
            "rulerLabel": f"{settings.BASE_CURRENCY['symbol']} {humanize_number(chosen_interval)}",
            "baseCurrency": settings.BASE_CURRENCY["symbol"],
            "theme": theme,
        })

        context["graph_data"] = graph_data
        context["total_asset_value"] = total_asset_value
        context["base_currency"] = settings.BASE_CURRENCY["symbol"]
        context["has_enough_data"] = True

        return context


class AssetsView(LoginRequiredMixin, TemplateView):
    template_name = "main/assets.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get valid range dates
        valid_range_start, valid_range_end = BalanceHistory.get_valid_range_dates(self.request.user)
        
        # Check if we have enough data points
        has_enough_data = (
            valid_range_start is not None 
            and valid_range_end is not None 
            and valid_range_start != valid_range_end
        )

        if not has_enough_data:
            context["has_enough_data"] = False
            context["donut_data"] = "{}"
            context["assets"] = []
            context["asset_classes"] = []
            context["relative_streamgraph_data"] = "{}"
            context["base_currency"] = settings.BASE_CURRENCY["symbol"]
            return context

        # Get latest balances
        balance_histories = BalanceHistory.get_valid_range_records(
            self.request.user
        ).values(
            "account__name",
            "account__color",
            "account__country",
            "balance",
            "asset",
            "date",
            "asset__asset_class__name",
            "asset__asset_class__color"
        )

        balance_histories_df = pd.DataFrame(list(balance_histories))
        balance_histories_df.rename(
            columns={
                "account__name": "account",
                "account__color": "account_color",
                "account__country": "account_country",
                "asset__asset_class__name": "asset_class",
                "asset__asset_class__color": "asset_class_color"
            },
            inplace=True,
        )

        # Calculate value history from BalanceHistory
        timespan_end = balance_histories_df["date"].max()
        timespan_start = balance_histories_df["date"].min()
        timespan = timespan_end - timespan_start

        # Collect prices by asset
        price_dfs = []
        for asset in balance_histories_df['asset'].unique():
            asset_daily_prices = get_prices_daily(
                instrument=asset,
                quote_currency=settings.BASE_CURRENCY["code"],
                timespan_end=timespan_end,
                timespan=timespan
            )
            temp_df = asset_daily_prices.reset_index().rename(columns={asset: 'price'})
            temp_df['asset'] = asset
            price_dfs.append(temp_df)

        # Combine all price dfs
        combined_prices_df = pd.concat(price_dfs, ignore_index=True)

        # Merge
        balance_histories_df = balance_histories_df.merge(combined_prices_df, on=['date', 'asset'], how='left')

        balance_histories_df["value"] = (
                balance_histories_df["price"] * balance_histories_df["balance"]
        )

        # Sum values of items of the same asset class
        grouping_field = "asset_class"
        grouped = balance_histories_df.groupby(
            ["date", grouping_field], as_index=False
        ).agg({"value": "sum"})
        pivot = grouped.pivot(
            index="date", columns=grouping_field, values="value"
        ).reset_index()

        # Get latest balances
        accounts_items = (
            BalanceHistory.get_latest_valid_balances(self.request.user)
        ).values(
            "account__name",
            "account__color",
            "account__country",
            "balance",
            "asset",
            "asset__name",
            "asset__asset_class__name",
            "asset__asset_class__color"
        )
        accounts_items_df = pd.DataFrame(list(accounts_items))
        accounts_items_df.rename(
            columns={
                "account__name": "account",
                "account__color": "account_color",
                "account__country": "account_country",
                "asset__asset_class__name": "asset_class",
                "asset__asset_class__color": "color",
                "asset__name": "name",
            },
            inplace=True,
        )

        # Calculate current values
        accounts_items_df["current_price"] = accounts_items_df.apply(
            lambda x: get_prices_daily(
                x["asset"],
                settings.BASE_CURRENCY["code"],
                datetime.datetime.now().date(),
            ),
            axis=1,
        )
        accounts_items_df["current_value"] = (
                accounts_items_df["current_price"] * accounts_items_df["balance"]
        )
        accounts_items_df.sort_values("asset_class", inplace=True)

        # Group by asset class
        d = {
            "asset_class_tot_value": ("current_value", "sum"),
            "color": ("color", "first"),
        }
        asset_class_sums = (
            accounts_items_df.groupby(["asset_class"]).agg(**d).reset_index()
        )
        accounts_items_df = accounts_items_df.merge(
            asset_class_sums.drop(columns=["color"]), on="asset_class"
        )
        asset_class_sums["allocation"] = (
                                                 asset_class_sums["asset_class_tot_value"]
                                                 / asset_class_sums["asset_class_tot_value"].sum()
                                         ) * 100

        # Generate themed colors for each asset class
        asset_class_sums["hsl_dark_background"] = asset_class_sums["color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 75%, 45%)"
        )
        asset_class_sums["hsl_light_background"] = asset_class_sums["color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 96%, 66%)"
        )

        d = {
            "asset_class_tot_value": ("asset_class_tot_value", "first"),
            "asset_class": ("asset_class", "first"),
            "color": ("color", "first"),
            "tot_current_value": ("current_value", "sum"),
            "tot_balance": ("balance", "sum"),
        }
        assets = accounts_items_df.groupby(["asset", "name"]).agg(**d).reset_index()

        # Generate themed colors for each asset
        assets["hsl_dark_background"] = assets["color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 75%, 45%)"
        )
        assets["hsl_light_background"] = assets["color"].apply(
            lambda x: f"hsl({hex_to_hsl_components(x)[0]}, 96%, 66%)"
        )
        assets["allocation"] = (
                                       assets["tot_current_value"] / assets["tot_current_value"].sum()
                               ) * 100

        tot_value = assets["tot_current_value"].sum()
        theme = self.request.theme
        opacity_val = opacity[theme]

        # Serialize relative streamgraph data for D3
        pivot_for_stream = pivot.copy()
        pivot_for_stream.set_index("date", inplace=True)
        df_norm = pivot_for_stream.div(pivot_for_stream.sum(axis=1), axis=0) * 100

        asset_class_color_idx = asset_class_sums.set_index("asset_class")

        rel_stream_series = []
        for col in df_norm.columns:
            fill_color = asset_class_color_idx.at[col, f"hsl_{theme}_background"]
            fill_color = fill_color.replace("hsl(", "hsla(").replace(
                ")", f",{opacity_val})"
            )
            rel_stream_series.append({
                "name": col,
                "values": df_norm[col].tolist(),
                "color": fill_color,
            })

        context["relative_streamgraph_data"] = json.dumps({
            "dates": [d.isoformat() for d in df_norm.index],
            "series": rel_stream_series,
        })

        # Serialize assets donut data for D3
        assets_sorted = assets.sort_values(
            ["asset_class_tot_value", "tot_current_value"]
        )
        assets_sorted["color"] = assets_sorted.groupby("asset_class")["color"].transform(
            lambda cs: generate_shades(cs.iloc[0], len(cs), theme=theme)
        )

        donut_slices = []
        for _, row in assets_sorted.iterrows():
            donut_slices.append({
                "label": row["asset"],
                "value": row["tot_current_value"],
                "color": row["color"],
            })

        context["donut_data"] = json.dumps({
            "slices": donut_slices,
            "centerText": f"{tot_value:,.2f} {settings.BASE_CURRENCY['symbol']}",
            "baseCurrency": settings.BASE_CURRENCY["symbol"],
            "hole": 0.7,
            "layout": "labels-outside",
        })

        # Pass table data to template
        context["assets"] = (
            assets.sort_values("tot_current_value", ascending=False)
            .drop(columns=["asset_class_tot_value"])
            .to_dict("records")
        )
        context["asset_classes"] = asset_class_sums.sort_values(
            "asset_class_tot_value", ascending=False
        ).to_dict("records")
        context["base_currency"] = settings.BASE_CURRENCY["symbol"]
        context["has_enough_data"] = True

        return context


class AccountsView(LoginRequiredMixin, TemplateView):
    template_name = "main/accounts.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Show accounts page if user has any accounts (even with no balance history)
        has_data = Account.objects.filter(user=self.request.user).exists()

        if not has_data:
            context["has_data"] = False
            context["accounts_donut_data"] = "{}"
            context["accounts_country_donut_data"] = "{}"
            context["accounts_table"] = []
            context["base_currency"] = settings.BASE_CURRENCY["symbol"]
            return context

        def theme_account_color(hex_color, s_change, l_change):
            h, s, l = hex_to_hsl_components(hex_color)
            s = max(0, min(100, s + (s_change * 100)))
            l = max(0, min(100, l + (l_change * 100)))
            return f"hsl({h}, {s}%, {l}%)"

        # Base: all user accounts with theme colors
        all_accounts = list(
            Account.objects.filter(user=self.request.user).values(
                "name", "color", "country"
            )
        )
        all_accounts_df = pd.DataFrame(all_accounts)
        all_accounts_df.rename(columns={"name": "account"}, inplace=True)
        all_accounts_df["account_color"] = all_accounts_df["color"]
        all_accounts_df["account_country"] = all_accounts_df["country"]
        all_accounts_df["hsl_dark_background"] = all_accounts_df["color"].apply(
            lambda x: theme_account_color(x, -0.05, -0.10)
        )
        all_accounts_df["hsl_light_background"] = all_accounts_df["color"].apply(
            lambda x: theme_account_color(x, 0.05, 0.10)
        )
        all_accounts_df.drop(columns=["color", "country"], inplace=True)

        # Latest balances (may be empty)
        latest_balances = BalanceHistory.get_latest_valid_balances(
            self.request.user
        ).values(
            "account__name",
            "account__color",
            "account__country",
            "balance",
            "asset",
        )
        balances_list = list(latest_balances)

        if balances_list:
            balances_df = pd.DataFrame(balances_list)
            balances_df.rename(
                columns={
                    "account__name": "account",
                    "account__color": "account_color",
                    "account__country": "account_country",
                },
                inplace=True,
            )
            today = timezone.localdate()
            balances_df["current_price"] = balances_df.apply(
                lambda x: get_prices_daily(
                    x["asset"],
                    settings.BASE_CURRENCY["code"],
                    today,
                ),
                axis=1,
            )
            balances_df["current_value"] = (
                balances_df["current_price"] * balances_df["balance"]
            )
            balances_df["hsl_dark_background"] = balances_df["account_color"].apply(
                lambda x: theme_account_color(x, -0.05, -0.10)
            )
            balances_df["hsl_light_background"] = balances_df["account_color"].apply(
                lambda x: theme_account_color(x, 0.05, 0.10)
            )
            value_agg = balances_df.groupby("account").agg(
                account_tot_value=("current_value", "sum"),
            ).reset_index()
            account_sums = all_accounts_df.merge(
                value_agg,
                on="account",
                how="left",
            )
            account_sums["account_tot_value"] = account_sums[
                "account_tot_value"
            ].fillna(0)
        else:
            account_sums = all_accounts_df.copy()
            account_sums["account_tot_value"] = 0.0

        total_value = account_sums["account_tot_value"].sum()
        account_sums["account_value_perc"] = (
            (account_sums["account_tot_value"] / total_value * 100)
            if total_value > 0
            else 0
        )

        # Transaction volume per account
        transactions = Transaction.objects.filter(
            account__user=self.request.user
        ).values("account__name", "amount", "datetime")
        transactions_df = pd.DataFrame(list(transactions))

        if not transactions_df.empty:
            transactions_df["date"] = pd.to_datetime(
                transactions_df["datetime"]
            ).dt.date
            grouped = (
                transactions_df.groupby(["account__name", "date"])
                .agg(
                    total_vol=("amount", lambda x: x.abs().sum()),
                    total_incoming=("amount", lambda x: x[x > 0].sum()),
                    total_outgoing=("amount", lambda x: -x[x < 0].sum()),
                )
                .reset_index()
            )
            daily_agg = (
                grouped.groupby("account__name")
                .agg(
                    avg_daily_volume=("total_vol", "mean"),
                    avg_daily_incoming=("total_incoming", "mean"),
                    avg_daily_outgoing=("total_outgoing", "mean"),
                )
                .reset_index()
            )
            daily_agg["avg_monthly_volume"] = daily_agg["avg_daily_volume"] * 30
            daily_agg["avg_monthly_incoming"] = (
                daily_agg["avg_daily_incoming"] * 30
            )
            daily_agg["avg_monthly_outgoing"] = (
                daily_agg["avg_daily_outgoing"] * 30
            )
            account_sums = account_sums.merge(
                daily_agg,
                left_on="account",
                right_on="account__name",
                how="left",
            ).drop(columns=["account__name"], errors="ignore")
            account_sums["avg_monthly_volume"] = account_sums[
                "avg_monthly_volume"
            ].fillna(0)
            account_sums["avg_monthly_incoming"] = account_sums[
                "avg_monthly_incoming"
            ].fillna(0)
            account_sums["avg_monthly_outgoing"] = account_sums[
                "avg_monthly_outgoing"
            ].fillna(0)
        else:
            account_sums["avg_monthly_volume"] = 0
            account_sums["avg_monthly_incoming"] = 0
            account_sums["avg_monthly_outgoing"] = 0

        account_sums.sort_values("account_tot_value", ascending=False, inplace=True)

        # Donuts: only accounts/countries with balance > 0 (reuse balances_df if we have balances)
        theme = self.request.theme
        if balances_list:
            accounts_df = balances_df.groupby("account").agg(
                account_total_value=("current_value", "sum"),
                hsl_dark_background=("hsl_dark_background", "first"),
                hsl_light_background=("hsl_light_background", "first"),
            ).reset_index()
            accounts_df = accounts_df[accounts_df["account_total_value"] > 0]
            accounts_df.sort_values("account_total_value", inplace=True)
            acct_slices = [
                {
                    "label": row["account"],
                    "value": row["account_total_value"],
                    "color": row[f"hsl_{theme}_background"],
                }
                for _, row in accounts_df.iterrows()
            ]
            accounts_country_df = (
                balances_df.groupby("account_country")
                .agg(account_total_value=("current_value", "sum"))
                .reset_index()
            )
            accounts_country_df = accounts_country_df[
                accounts_country_df["account_total_value"] > 0
            ]
            accounts_country_df.sort_values("account_total_value", inplace=True)
            country_colors = ["#2ab668", "#3255a4"]
            country_slices = [
                {
                    "label": row["account_country"],
                    "value": row["account_total_value"],
                    "color": country_colors[i % len(country_colors)],
                }
                for i, (_, row) in enumerate(accounts_country_df.iterrows())
            ]
        else:
            acct_slices = []
            country_slices = []

        context["accounts_donut_data"] = json.dumps({
            "slices": acct_slices,
            "baseCurrency": settings.BASE_CURRENCY["symbol"],
            "hole": 0.6,
            "layout": "legend-right",
        })
        context["accounts_country_donut_data"] = json.dumps({
            "slices": country_slices,
            "baseCurrency": settings.BASE_CURRENCY["symbol"],
            "hole": 0.6,
            "layout": "legend-right",
        })
        context["accounts_donut_has_slices"] = len(acct_slices) > 0
        context["accounts_country_donut_has_slices"] = len(country_slices) > 0
        context["accounts_table"] = account_sums.to_dict("records")
        context["base_currency"] = settings.BASE_CURRENCY["symbol"]
        context["has_data"] = True

        return context


class TransactionsView(LoginRequiredMixin, ListView):
    model = Transaction
    template_name = "main/transactions.html"
    login_url = "login"
    context_object_name = "transactions"

    def get_queryset(self):
        # Get user's transactions (prefetch related fields)
        return (
            Transaction.objects.filter(account__user=self.request.user)
            .select_related("asset", "account")
            .order_by("-datetime")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        transactions = context["transactions"]

        # Get all assets that appear in transactions, sorted by descending txn count
        context["assets"] = (
            Asset.objects.filter(transaction__account__user=self.request.user)
            .annotate(transaction_count=Count("transaction"))
            .order_by("-transaction_count")
            .distinct()
        )

        # Get all accounts sorted by descending txn count
        context["accounts"] = (
            Account.objects.filter(user=self.request.user)
            .annotate(transaction_count=Count("transaction"))
            .order_by("-transaction_count")
            .distinct()
        )

        # Format transactions account badge color and string
        formatted_transactions = []
        for txn in transactions:
            txn.formatted_amount = "{:,.2f}".format(abs(txn.amount))
            txn.asset.symbol = getattr(txn.asset, "symbol", txn.asset.name)

            h, s, l = hex_to_hsl_components(txn.account.color)
            txn.hsl_dark_background = f"hsl({h}, 50%, 29%)"
            txn.hsl_dark_text = f"hsl({h}, 100%, 85%)"
            txn.hsl_light_background = f"hsl({h}, 93%, 85%)"
            txn.hsl_light_text = f"hsl({h}, 68%, 37%)"

            formatted_transactions.append(txn)

        context["transactions"] = formatted_transactions
        context["today"] = timezone.localdate()
        context["base_currency"] = settings.BASE_CURRENCY["symbol"]

        return context


class EarningsView(LoginRequiredMixin, TemplateView):
    template_name = "main/earnings.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        transactions = Transaction.objects.filter(
            amount__gt=0, account__user=self.request.user
        )

        # Convert transactions to DataFrame
        data = transactions.values("datetime", "entity", "amount")
        df = pd.DataFrame(data)

        # Check if we have enough data points (at least 2 transactions)
        if len(df) < 2:
            context["barplot_data"] = "{}"
            context["earnings_sources"] = []
            context["base_currency"] = settings.BASE_CURRENCY["symbol"]
            context["has_enough_data"] = False
            return context

        # Generate colors for each unique entity
        unique_entities = df["entity"].unique()
        colors_map = {
            "light": {},
            "dark": {}
        }
        
        # Generate a different hue for each entity, evenly distributed
        hue_step = 360 / (len(unique_entities) or 1)  # Avoid division by zero
        for idx, entity in enumerate(unique_entities):
            hue = int(idx * hue_step)
            # Light theme: high saturation and lightness
            colors_map["light"][entity] = f"hsl({hue}, 88%, 60%)"
            # Dark theme: lower saturation and lightness for better contrast
            colors_map["dark"][entity] = f"hsl({hue}, 55%, 45%)"

        df["color"] = df["entity"].map(colors_map[self.request.theme])

        # Convert datetimes to user timezone for correct month grouping, then strip tz for pandas
        user_tz = str(timezone.get_current_timezone())
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_convert(user_tz).dt.tz_localize(None)

        # Extract the month from datetime
        df["month"] = df["datetime"].dt.to_period("M").dt.to_timestamp()

        # Group by 'month' and 'entity' with sum of 'amount'
        grouped_df = (
            df.groupby(["month", "entity", "color"])["amount"].sum().reset_index()
        )

        # Calculate the start_month for x-axis alignment
        start_month = grouped_df["month"].min()

        # Initialize a list to store trend data
        trend_data = []
        entities = grouped_df["entity"].unique()
        total_volume = grouped_df["amount"].sum()

        for entity in entities:
            df_entity = grouped_df[grouped_df["entity"] == entity].copy()

            color = df_entity["color"].tolist()[0]

            # Compute 'x' as months since start_month
            df_entity["x"] = (df_entity["month"].dt.year - start_month.year) * 12 + (
                    df_entity["month"].dt.month - start_month.month
            )

            x = df_entity["x"].values
            y = df_entity["amount"].values

            # Linear regression to find m
            if len(x) > 1:
                m, _ = np.polyfit(x, y, 1)
            else:
                m, _ = 0, y[0]  # Default values if not enough data points

            total_volume_entity = df_entity["amount"].sum()
            mean_y = y.mean() if len(y) > 0 else 0

            monthly_percentage_increase = (m / mean_y) * 100 if mean_y > 0 else 0
            yearly_percentage_increase = monthly_percentage_increase * 12

            trend_data.append(
                {
                    "entity": entity,
                    "delta_monthly": m,
                    "delta_yearly": m * 12,
                    "delta_monthly_percentage": monthly_percentage_increase,
                    "delta_yearly_percentage": yearly_percentage_increase,
                    "total_volume_entity": total_volume_entity,
                    "total_volume_entity_percentage": (
                                                              total_volume_entity / total_volume
                                                      )
                                                      * 100,
                    "color": color,
                }
            )

        # Create DataFrame for trend data
        df_trends = pd.DataFrame(trend_data).sort_values(
            "total_volume_entity_percentage", ascending=False
        )
        context["earnings_sources"] = df_trends.to_dict("records")

        # Serialize barplot data for D3
        grouped_df["month_str"] = grouped_df["month"].apply(
            lambda x: pd.Timestamp(x).strftime("%b %Y")
        )
        bar_records = []
        for _, row in grouped_df.iterrows():
            bar_records.append({
                "month": row["month_str"],
                "entity": row["entity"],
                "amount": row["amount"],
                "color": row["color"],
            })

        context["barplot_data"] = json.dumps({
            "records": bar_records,
            "baseCurrency": settings.BASE_CURRENCY["symbol"],
        })
        context["base_currency"] = settings.BASE_CURRENCY["symbol"]
        context["has_enough_data"] = True

        return context


# ── GoCardless Bank Connection Views ──────────────────────────────────────────



class BankLinkView(LoginRequiredMixin, View):
    """Step 2: Initiate connection for the selected bank."""
    login_url = "login"

    def post(self, request, *args, **kwargs):
        from main.services.stateful.gocardless.connection import ConnectionService

        institution_id = request.POST.get("institution_id", "")
        name = request.POST.get("name", "")
        country = request.POST.get("country", "")
        transaction_total_days = int(request.POST.get("transaction_total_days", 90))

        if not institution_id or not name:
            messages.error(request, "Invalid institution selection.")
            return redirect(f"{reverse('configuration')}?step=bank_select")

        # Build the callback URL
        redirect_url = request.build_absolute_uri("/bank/callback/")

        try:
            svc = ConnectionService()
            requisition = svc.initiate_connection(
                user=request.user,
                institution_id=institution_id,
                institution_name=name,
                institution_country=country,
                transaction_total_days=transaction_total_days,
                redirect_url=redirect_url,
            )
        except Exception as exc:
            messages.error(request, f"Could not initiate bank connection: {exc}")
            return redirect(f"{reverse('configuration')}?step=bank_select")

        # Redirect user to GoCardless/bank authentication page
        return redirect(requisition.link)


class BankCallbackView(LoginRequiredMixin, View):
    """Step 3: GoCardless redirects back here after bank authentication."""
    login_url = "login"

    def get(self, request, *args, **kwargs):
        import logging
        from main.services.stateful.gocardless.connection import ConnectionService

        logger = logging.getLogger(__name__)
        ref = request.GET.get("ref", "")

        # Find the requisition for this user (most recent pending one)
        requisition = (
            Requisition.objects.filter(user=request.user)
            .exclude(status__in=["LN", "EX", "RJ"])
            .order_by("-created_at")
            .first()
        )

        if not requisition:
            logger.warning("BankCallbackView: No pending requisition found for user %s", request.user.pk)
            messages.error(request, "No pending bank connection found.")
            return redirect("configuration")

        logger.info("BankCallbackView: Processing requisition %s for user %s", requisition.pk, request.user.pk)

        try:
            svc = ConnectionService()
            accounts = svc.complete_connection(str(requisition.pk))
            logger.info("BankCallbackView: Successfully created %d account(s) from requisition %s", len(accounts), requisition.pk)
            messages.success(
                request,
                f"Successfully linked {len(accounts)} account(s) from {requisition.institution_name}. "
                f"Initial sync is running in the background.",
            )
        except Exception as exc:
            logger.exception("BankCallbackView: Failed to complete connection for requisition %s", requisition.pk)
            messages.error(request, f"Bank connection failed: {exc}")

        return redirect("configuration")


class SyncAccountView(LoginRequiredMixin, View):
    """Trigger a sync for a specific account. Returns JSON status."""
    login_url = "login"

    def post(self, request, *args, **kwargs):
        import logging
        import threading
        from django.http import JsonResponse
        from main.services.stateful.gocardless.sync import (
            TransactionSyncService,
            SyncAlreadyInProgressError,
        )

        logger = logging.getLogger(__name__)

        account_id = kwargs.get("account_id")
        try:
            account = Account.objects.get(pk=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"error": "Account not found"}, status=404)

        if not account.is_synced:
            return JsonResponse({"error": "Account is not a synced account"}, status=400)

        def _run():
            try:
                syncer = TransactionSyncService()
                syncer.sync_account(account)
            except SyncAlreadyInProgressError:
                # Another sync is already running - this is expected in race conditions
                logger.info("Sync already in progress for account %s", account.pk)
            except Exception:
                pass  # Error is written to account.sync_error by the service

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        return JsonResponse({"status": "sync_started"})


class SyncStatusView(LoginRequiredMixin, View):
    """Poll endpoint for sync status of an account."""
    login_url = "login"

    def get(self, request, *args, **kwargs):
        from django.http import JsonResponse

        account_id = kwargs.get("account_id")
        try:
            account = Account.objects.get(pk=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"error": "Account not found"}, status=404)

        return JsonResponse({
            "sync_status": account.sync_status,
            "sync_error": account.sync_error,
            "last_successful_sync": (
                account.last_successful_sync.isoformat()
                if account.last_successful_sync else None
            ),
        })


class DeleteAccountView(LoginRequiredMixin, View):
    """Delete an account (synced or manual) and all its data."""
    login_url = "login"

    def post(self, request, *args, **kwargs):
        from django.http import JsonResponse

        account_id = kwargs.get("account_id")
        try:
            account = Account.objects.get(pk=account_id, user=request.user)
        except Account.DoesNotExist:
            messages.error(request, "Account not found.")
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)

        account_name = account.name
        account.delete()  # CASCADE will delete transactions, balance history, etc.
        
        messages.success(request, f"Account '{account_name}' and all its data have been deleted.")
        return JsonResponse({"success": True})


class AddAccountView(LoginRequiredMixin, TemplateView):
    template_name = "main/add_account.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_view"] = "configuration"
        context["gocardless_enabled"] = settings.GOCARDLESS_ENABLED
        return context


class AddManualAccountView(LoginRequiredMixin, View):
    template_name = "main/add_manual_account.html"
    login_url = "login"

    def _render_form(self, request, form_data=None):
        return render(request, self.template_name, {
            "current_view": "configuration",
            "countries": COUNTRY_CHOICES,
            "form_data": form_data or {
                "name": "",
                "country": "",
                "color": "#4b8dd3",
            },
        })

    def get(self, request, *args, **kwargs):
        return self._render_form(request)

    def post(self, request, *args, **kwargs):
        name = request.POST.get("name", "").strip()
        country = request.POST.get("country", "").strip()
        color = request.POST.get("color", "#4b8dd3").strip()

        form_data = {
            "name": name,
            "country": country,
            "color": color,
        }

        if not name:
            messages.error(request, "Account name is required.")
            return self._render_form(request, form_data)

        valid_countries = {choice[0] for choice in COUNTRY_CHOICES}
        if country not in valid_countries:
            messages.error(request, "Please select a valid country.")
            return self._render_form(request, form_data)

        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            messages.error(request, "Please choose a valid color.")
            return self._render_form(request, form_data)

        try:
            account = Account.objects.create(
                user=request.user,
                name=name,
                country=country,
                color=color,
            )
        except IntegrityError:
            messages.error(request, "An account with this name already exists.")
            return self._render_form(request, form_data)

        messages.success(request, f"Manual account '{name}' created.")
        return redirect("configuration")


class ConfigurationView(LoginRequiredMixin, TemplateView):
    """Configuration page for managing all accounts (synced and manual)."""
    template_name = "main/configuration.html"
    login_url = "login"

    def get_context_data(self, **kwargs):
        from main.constants import COUNTRY_CHOICES

        context = super().get_context_data(**kwargs)
        context["current_view"] = "configuration"
        context["gocardless_enabled"] = settings.GOCARDLESS_ENABLED

        # Get all accounts (both synced and manual) with theme-adjusted dot colors
        all_accounts = list(
            Account.objects.filter(user=self.request.user).order_by("name")
        )

        def _theme_account_color(hex_color, s_change, l_change):
            h, s, l = hex_to_hsl_components(hex_color or "#4b8dd3")
            s = max(0, min(100, s + (s_change * 100)))
            l = max(0, min(100, l + (l_change * 100)))
            return f"hsl({h}, {s}%, {l}%)"

        for account in all_accounts:
            account.hsl_light_background = _theme_account_color(
                account.color, 0.05, 0.10
            )
            account.hsl_dark_background = _theme_account_color(
                account.color, -0.05, -0.10
            )

        context["all_accounts"] = all_accounts

        # Check if we're in bank selection step
        step = self.request.GET.get("step", "")
        context["step"] = step

        if step == "bank_select":
            context["countries"] = COUNTRY_CHOICES
            context["institutions"] = []
            context["selected_country"] = self.request.GET.get("country", "")
        
        # Tab navigation (default to "accounts")
        active_tab = self.request.GET.get("tab", "accounts")
        context["active_tab"] = active_tab
        
        # Load data for transactions tab
        if active_tab == "transactions":
            # Get all fiat assets for filter dropdown
            context["fiat_assets"] = Asset.objects.filter(asset_class__name="Fiat Currencies").order_by("code")
            
            # Get all user accounts (manual only) for filter dropdown
            context["user_accounts"] = Account.objects.filter(user=self.request.user, gocardless_code__isnull=True).order_by("name")
            
            # Get filtered transactions
            account_filter = self.request.GET.get("account")
            asset_filter = self.request.GET.get("asset")
            
            txns = Transaction.objects.filter(account__user=self.request.user).select_related("account", "asset")
            
            if account_filter:
                txns = txns.filter(account_id=account_filter)
            if asset_filter:
                txns = txns.filter(asset__code=asset_filter)
            
            context["transactions"] = txns.order_by("-datetime")
            context["selected_account"] = account_filter
            context["selected_asset"] = asset_filter
        
        # Load data for trades tab
        elif active_tab == "trades":
            # Get all user accounts (manual only) for filter dropdown
            context["user_accounts"] = Account.objects.filter(user=self.request.user, gocardless_code__isnull=True).order_by("name")
            context["all_assets"] = Asset.objects.order_by("code")
            
            # Get filtered trades
            account_filter = self.request.GET.get("account")
            
            # Query for trade sides and group them
            trades_query = Transaction.objects_extended.filter(
                account__user=self.request.user,
                is_trade_side=True
            ).select_related("account", "asset", "paired_transaction", "paired_transaction__asset")
            
            if account_filter:
                trades_query = trades_query.filter(account_id=account_filter)
            
            # Group paired transactions into single trade entries
            # We'll only show each trade once (using the first transaction of each pair)
            trades = []
            seen_ids = set()
            
            for txn in trades_query.order_by("-datetime"):
                if txn.id in seen_ids:
                    continue
                
                paired = txn.paired_transaction
                if paired:
                    seen_ids.add(txn.id)
                    seen_ids.add(paired.id)
                    
                    # Determine which is "from" (negative) and which is "to" (positive)
                    if txn.amount < 0:
                        from_txn, to_txn = txn, paired
                    else:
                        from_txn, to_txn = paired, txn
                    
                    trades.append({
                        "id": txn.id,  # Use either ID for deletion
                        "datetime": txn.datetime,
                        "account": txn.account,
                        "entity": txn.entity,
                        "from_asset": from_txn.asset,
                        "from_amount": abs(from_txn.amount),
                        "to_asset": to_txn.asset,
                        "to_amount": to_txn.amount,
                    })
            
            context["trades"] = trades
            context["selected_account"] = account_filter

        return context

    def post(self, request, *args, **kwargs):
        """Handle bank selection form submission."""
        from main.services.stateful.gocardless.connection import ConnectionService
        from main.services.stateful.gocardless.constants import COUNTRY_TO_ISO_CODE
        from main.constants import COUNTRY_CHOICES

        step = request.GET.get("step", "")
        if step != "bank_select":
            return redirect("configuration")

        country_name = request.POST.get("country_name", "").strip()
        if not country_name:
            messages.error(request, "Please select a valid country.")
            return redirect(f"{reverse('configuration')}?step=bank_select")

        country_code = COUNTRY_TO_ISO_CODE.get(country_name)
        if not country_code:
            messages.error(request, f"Country '{country_name}' not recognized.")
            return redirect(f"{reverse('configuration')}?step=bank_select")

        try:
            svc = ConnectionService()
            institutions = svc.list_institutions(country_code)
        except Exception as exc:
            messages.error(request, f"Could not fetch institutions: {exc}")
            return redirect(f"{reverse('configuration')}?step=bank_select")

        context = self.get_context_data(**kwargs)
        context["institutions"] = institutions
        context["selected_country"] = country_name
        return self.render_to_response(context)


class ManageAccountAssetsView(LoginRequiredMixin, TemplateView):
    """Shows established assets for a manual account and allows adding new ones."""
    template_name = "main/manage_account_assets.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account_id = kwargs.get("account_id")

        try:
            account = Account.objects.get(id=account_id, user=self.request.user)
        except Account.DoesNotExist:
            raise Http404("Account not found")

        if account.is_synced:
            raise PermissionDenied("Cannot manually manage assets for synced accounts")

        # Get established assets with their latest balance via AccountAsset
        from main.models import AccountAsset
        
        account_assets = AccountAsset.objects.filter(account=account).select_related("asset")
        established_codes = [aa.asset.code for aa in account_assets]
        
        established_assets = []
        for account_asset in account_assets.order_by("asset__code"):
            asset = account_asset.asset
            latest_bh = (
                BalanceHistory.objects
                .filter(account=account, asset=asset)
                .order_by("-date")
                .first()
            )
            established_assets.append({
                "asset": asset,
                "balance": latest_bh.balance if latest_bh else 0,
                "date": latest_bh.date if latest_bh else None,
            })

        # All assets available for "add asset" (excluding already-established ones)
        available_assets = Asset.objects.exclude(code__in=established_codes).order_by("code")

        context["account"] = account
        context["established_assets"] = established_assets
        context["available_assets"] = available_assets
        context["current_view"] = "configuration"
        return context


class AddAssetBalanceView(LoginRequiredMixin, View):
    """Establishes a new asset on a manual account by creating AccountAsset and flat BH."""
    def post(self, request, account_id):
        from main.models import AccountAsset
        
        try:
            account = Account.objects.get(id=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)

        if account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot manually manage synced accounts"}, status=403)

        asset_code = request.POST.get("asset", "").strip()
        balance_str = request.POST.get("balance", "").strip()

        if not asset_code:
            return JsonResponse({"success": False, "error": "Asset is required"}, status=400)

        try:
            asset = Asset.objects.get(pk=asset_code)
        except Asset.DoesNotExist:
            return JsonResponse({"success": False, "error": "Invalid asset"}, status=400)

        # Check if already established
        if AccountAsset.objects.filter(account=account, asset=asset).exists():
            return JsonResponse({"success": False, "error": "This asset is already established on this account"}, status=400)

        if not balance_str:
            return JsonResponse({"success": False, "error": "Balance is required"}, status=400)

        try:
            balance = float(balance_str)
            if balance < 0:
                return JsonResponse({"success": False, "error": "Balance cannot be negative"}, status=400)
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid balance format"}, status=400)

        with transaction.atomic():
            # Create AccountAsset entry
            AccountAsset.objects.create(account=account, asset=asset)
            
            # Create flat balance history from BALANCE_HISTORY_TIMESPAN_DAYS ago to today
            today = timezone.localdate()
            start_date = today - datetime.timedelta(days=settings.BALANCE_HISTORY_TIMESPAN_DAYS)
            
            entries = []
            current = start_date
            while current <= today:
                entries.append(
                    BalanceHistory(account=account, asset=asset, date=current, balance=balance)
                )
                current += datetime.timedelta(days=1)
            
            BalanceHistory.objects.bulk_create(entries)

        return JsonResponse({"success": True})


class CorrectBalanceView(LoginRequiredMixin, View):
    """Corrects the current balance of an established asset by applying a delta to entire history."""
    def post(self, request, account_id, asset_code):
        try:
            account = Account.objects.get(id=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)

        if account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot manually manage synced accounts"}, status=403)

        try:
            asset = Asset.objects.get(pk=asset_code)
        except Asset.DoesNotExist:
            return JsonResponse({"success": False, "error": "Invalid asset"}, status=400)

        balance_str = request.POST.get("balance", "").strip()
        if not balance_str:
            return JsonResponse({"success": False, "error": "Balance is required"}, status=400)

        try:
            new_balance = float(balance_str)
            if new_balance < 0:
                return JsonResponse({"success": False, "error": "Balance cannot be negative"}, status=400)
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid balance format"}, status=400)

        # Get current balance
        latest_bh = (
            BalanceHistory.objects
            .filter(account=account, asset=asset)
            .order_by("-date")
            .first()
        )
        if not latest_bh:
            return JsonResponse({"success": False, "error": "No balance history exists for this asset"}, status=400)

        # Calculate delta and apply to all BH entries
        delta = new_balance - latest_bh.balance
        
        try:
            with transaction.atomic():
                BalanceHistory.objects.filter(
                    account=account, asset=asset
                ).update(balance=F("balance") + delta)

                # Validate: no negative balances
                if BalanceHistory.objects.filter(account=account, asset=asset, balance__lt=0).exists():
                    raise ValidationError("Balance correction would cause negative historical balances")
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": str(exc.message)}, status=400)

        return JsonResponse({"success": True})


class DeleteAssetBalanceView(LoginRequiredMixin, View):
    """Deletes all balance history for an asset on a manual account."""
    def post(self, request, account_id, asset_code):
        try:
            account = Account.objects.get(id=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)

        if account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot manually manage synced accounts"}, status=403)

        try:
            asset = Asset.objects.get(pk=asset_code)
        except Asset.DoesNotExist:
            return JsonResponse({"success": False, "error": "Invalid asset"}, status=400)

        # Delete all balance history, transactions, and AccountAsset for this asset
        from main.models import AccountAsset
        
        with transaction.atomic():
            Transaction.objects_extended.filter(account=account, asset=asset).delete()
            BalanceHistory.objects.filter(account=account, asset=asset).delete()
            AccountAsset.objects.filter(account=account, asset=asset).delete()

        return JsonResponse({"success": True})


class ManageAccountTransactionsView(LoginRequiredMixin, TemplateView):
    """Per-asset transaction management for a manual account."""
    template_name = "main/manage_account_transactions.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        account_id = kwargs.get("account_id")
        asset_code = kwargs.get("asset_code")

        try:
            account = Account.objects.get(id=account_id, user=self.request.user)
        except Account.DoesNotExist:
            raise Http404("Account not found")

        if account.is_synced:
            raise PermissionDenied("Cannot manually manage transactions for synced accounts")

        try:
            asset = Asset.objects.get(pk=asset_code)
        except Asset.DoesNotExist:
            raise Http404("Asset not found")

        # Verify this asset is established on this account
        if not BalanceHistory.objects.filter(account=account, asset=asset).exists():
            raise Http404("Asset not established on this account")

        transactions = (
            Transaction.objects
            .filter(account=account, asset=asset)
            .order_by("-datetime")
        )

        context["account"] = account
        context["asset"] = asset
        context["transactions"] = transactions
        context["current_view"] = "configuration"
        return context


class AddTransactionView(LoginRequiredMixin, View):
    """Adds a transaction and applies the delta to BH entries from the transaction date onward."""
    def post(self, request, account_id, asset_code):
        try:
            account = Account.objects.get(id=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)

        if account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot manually add transactions to synced accounts"}, status=403)

        try:
            asset = Asset.objects.get(pk=asset_code)
        except Asset.DoesNotExist:
            return JsonResponse({"success": False, "error": "Invalid asset"}, status=400)

        entity = request.POST.get("entity", "").strip()
        amount_str = request.POST.get("amount", "").strip()
        datetime_str = request.POST.get("datetime", "").strip()
        category = request.POST.get("category", "").strip() or None
        influences_balance = request.POST.get("influences_balance", "1") == "1"

        if not entity:
            return JsonResponse({"success": False, "error": "From/to is required"}, status=400)

        if not amount_str:
            return JsonResponse({"success": False, "error": "Amount is required"}, status=400)

        try:
            amount = float(amount_str)
            if amount == 0:
                return JsonResponse({"success": False, "error": "Amount cannot be zero"}, status=400)
        except ValueError:
            return JsonResponse({"success": False, "error": "Invalid amount format"}, status=400)

        if not datetime_str:
            return JsonResponse({"success": False, "error": "Date/Time is required"}, status=400)

        try:
            transaction_datetime = parse_datetime(datetime_str)
            if not transaction_datetime:
                return JsonResponse({"success": False, "error": "Invalid datetime format"}, status=400)
            # Front-end attaches the browser UTC offset, so this path is a safety fallback only.
            if timezone.is_naive(transaction_datetime):
                transaction_datetime = timezone.make_aware(
                    transaction_datetime, timezone.get_current_timezone()
                )
        except Exception:
            return JsonResponse({"success": False, "error": "Invalid datetime format"}, status=400)

        txn_date = transaction_datetime.date()
        
        # Check if date is in the future (compare dates only to avoid timezone confusion)
        if txn_date > datetime.date.today():
            return JsonResponse({"success": False, "error": "Transaction date cannot be in the future"}, status=400)

        try:
            svc = TransactionService()
            svc.create_transaction_with_balance_update(
                account=account,
                entity=entity,
                amount=amount,
                datetime_obj=transaction_datetime,
                asset=asset,
                category=category,
                influences_balance=influences_balance,
                is_trade_side=False,
                paired_transaction=None,
            )
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": str(exc.message)}, status=400)

        return JsonResponse({"success": True})


class DeleteTransactionView(LoginRequiredMixin, View):
    """Deletes a transaction and reverses the delta from BH entries."""
    def post(self, request, transaction_id):
        try:
            txn = Transaction.objects.select_related("account", "asset").get(id=transaction_id)
        except Transaction.DoesNotExist:
            return JsonResponse({"success": False, "error": "Transaction not found"}, status=404)

        if txn.account.user != request.user:
            return JsonResponse({"success": False, "error": "Permission denied"}, status=403)

        if txn.account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot delete transactions from synced accounts"}, status=403)

        try:
            svc = TransactionService()
            svc.delete_transaction_with_balance_update(txn)
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": str(exc.message)}, status=400)

        return JsonResponse({"success": True})


class AddTradeView(LoginRequiredMixin, View):
    """Creates a trade as two paired transactions with balance history updates."""
    def post(self, request):
        account_id = request.POST.get("account_id")
        entity = ""
        datetime_str = request.POST.get("datetime", "").strip()
        
        # Input mode: either two amounts or amount+rate
        from_asset_code = request.POST.get("from_asset", "").strip()
        to_asset_code = request.POST.get("to_asset", "").strip()
        from_amount_str = request.POST.get("from_amount", "").strip()
        to_amount_str = request.POST.get("to_amount", "").strip()
        rate_str = request.POST.get("rate", "").strip()
        use_market_rate = request.POST.get("use_market_rate", "0") == "1"
        
        # Validate account
        try:
            account = Account.objects.get(id=account_id, user=request.user)
        except Account.DoesNotExist:
            return JsonResponse({"success": False, "error": "Account not found"}, status=404)
        
        if account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot manually add trades to synced accounts"}, status=403)
        
        if not datetime_str:
            return JsonResponse({"success": False, "error": "Date/Time is required"}, status=400)
        
        if not from_asset_code or not to_asset_code:
            return JsonResponse({"success": False, "error": "Both assets are required"}, status=400)
        
        # Front-end attaches the browser UTC offset; naive fallback retained for safety.
        try:
            trade_datetime = parse_datetime(datetime_str)
            if not trade_datetime:
                return JsonResponse({"success": False, "error": "Invalid datetime format"}, status=400)
            if timezone.is_naive(trade_datetime):
                trade_datetime = timezone.make_aware(
                    trade_datetime, timezone.get_current_timezone()
                )
        except Exception:
            return JsonResponse({"success": False, "error": "Invalid datetime format"}, status=400)
        
        trade_date = trade_datetime.date()
        
        # Check if date is in the future
        if trade_date > datetime.date.today():
            return JsonResponse({"success": False, "error": "Trade date cannot be in the future"}, status=400)
        
        # Validate assets exist
        try:
            from_asset = Asset.objects.get(pk=from_asset_code)
            to_asset = Asset.objects.get(pk=to_asset_code)
        except Asset.DoesNotExist:
            return JsonResponse({"success": False, "error": "Invalid asset"}, status=400)

        if from_asset_code == to_asset_code:
            return JsonResponse({"success": False, "error": "From and To assets must be different"}, status=400)
        
        try:
            from_amount = float(from_amount_str) if from_amount_str else None
            to_amount = float(to_amount_str) if to_amount_str else None
            rate = float(rate_str) if rate_str else None

            if use_market_rate:
                if rate_str:
                    return JsonResponse(
                        {"success": False, "error": "Rate must be empty when using app market rate"},
                        status=400,
                    )

                filled_amounts = sum([bool(from_amount_str), bool(to_amount_str)])
                if filled_amounts != 1:
                    return JsonResponse(
                        {"success": False, "error": "Enter exactly one amount when using app market rate"},
                        status=400,
                    )

                from_price_eur = float(get_prices_daily(from_asset.code, "EUR", trade_date).iloc[-1])
                to_price_eur = float(get_prices_daily(to_asset.code, "EUR", trade_date).iloc[-1])
                if from_price_eur <= 0 or to_price_eur <= 0:
                    return JsonResponse({"success": False, "error": "Invalid app market rate for selected assets"}, status=400)

                if from_amount is not None:
                    from_abs = abs(from_amount)
                    if from_abs == 0:
                        return JsonResponse({"success": False, "error": "Amounts cannot be zero"}, status=400)
                    to_abs = from_abs * (from_price_eur / to_price_eur)
                else:
                    to_abs = abs(to_amount)
                    if to_abs == 0:
                        return JsonResponse({"success": False, "error": "Amounts cannot be zero"}, status=400)
                    from_abs = to_abs * (to_price_eur / from_price_eur)

                from_amount = -from_abs
                to_amount = to_abs
            else:
                filled_values = sum([bool(from_amount_str), bool(to_amount_str), bool(rate_str)])

                if filled_values < 2:
                    return JsonResponse(
                        {"success": False, "error": "Provide either two amounts OR one amount with rate"},
                        status=400,
                    )

                if filled_values == 3:
                    return JsonResponse(
                        {"success": False, "error": "Do not provide all three values (two amounts + rate)"},
                        status=400,
                    )

                if from_amount and to_amount and not rate:
                    pass
                elif from_amount and rate and not to_amount:
                    if rate <= 0:
                        return JsonResponse({"success": False, "error": "Rate must be greater than zero"}, status=400)
                    to_amount = from_amount * rate
                elif to_amount and rate and not from_amount:
                    if rate <= 0:
                        return JsonResponse({"success": False, "error": "Rate must be greater than zero"}, status=400)
                    from_amount = to_amount / rate
                else:
                    return JsonResponse({"success": False, "error": "Invalid combination of values"}, status=400)

                if from_amount == 0 or to_amount == 0:
                    return JsonResponse({"success": False, "error": "Amounts cannot be zero"}, status=400)

                from_amount = -abs(from_amount)
                to_amount = abs(to_amount)

        except (ValueError, ZeroDivisionError):
            return JsonResponse({"success": False, "error": "Invalid amount or rate format"}, status=400)
        except Exception:
            return JsonResponse(
                {"success": False, "error": "App market rate is not available for the selected date/assets"},
                status=400,
            )
        
        try:
            svc = TransactionService()
            
            # Create first side of trade (the "from" side, negative amount)
            txn1 = svc.create_transaction_with_balance_update(
                account=account,
                entity=entity,
                amount=from_amount,
                datetime_obj=trade_datetime,
                asset=from_asset,
                category=None,
                influences_balance=True,
                is_trade_side=True,
                paired_transaction=None,
            )
            
            # Create second side of trade (the "to" side, positive amount)
            txn2 = svc.create_transaction_with_balance_update(
                account=account,
                entity=entity,
                amount=to_amount,
                datetime_obj=trade_datetime,
                asset=to_asset,
                category=None,
                influences_balance=True,
                is_trade_side=True,
                paired_transaction=txn1,
            )
            
            # Link first transaction back to second
            txn1.paired_transaction = txn2
            txn1.save()
            
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": str(exc.message)}, status=400)
        
        return JsonResponse({"success": True})


class DeleteTradeView(LoginRequiredMixin, View):
    """Deletes both sides of a trade transaction."""
    def post(self, request, trade_id):
        try:
            txn = Transaction.objects_extended.select_related("account", "asset", "paired_transaction").get(id=trade_id)
        except Transaction.DoesNotExist:
            return JsonResponse({"success": False, "error": "Trade not found"}, status=404)
        
        if txn.account.user != request.user:
            return JsonResponse({"success": False, "error": "Permission denied"}, status=403)
        
        if txn.account.is_synced:
            return JsonResponse({"success": False, "error": "Cannot delete trades from synced accounts"}, status=403)
        
        if not txn.is_trade_side:
            return JsonResponse({"success": False, "error": "Transaction is not a trade"}, status=400)
        
        paired_txn = txn.paired_transaction
        if not paired_txn:
            return JsonResponse({"success": False, "error": "Paired transaction not found"}, status=404)
        
        try:
            svc = TransactionService()
            # Delete both sides atomically
            with transaction.atomic():
                svc.delete_transaction_with_balance_update(txn)
                svc.delete_transaction_with_balance_update(paired_txn)
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": str(exc.message)}, status=400)
        
        return JsonResponse({"success": True})


def _build_timezone_choices():
    """Build a sorted list of (value, label) tuples for the timezone selector.
    
    Uses geographical timezone names (IANA) rather than fixed UTC offsets.
    IANA timezones automatically handle Daylight Saving Time transitions,
    ensuring dates and times are always displayed and interpreted correctly.
    """
    all_tzs = sorted(zoneinfo.available_timezones())
    choices = [(tz_name, tz_name) for tz_name in all_tzs]
    return choices

TIMEZONE_CHOICES = _build_timezone_choices()


class SettingsView(LoginRequiredMixin, View):
    """User settings page."""
    login_url = "login"

    def get(self, request, *args, **kwargs):
        context = {
            "current_view": "settings",
            "user": request.user,
            "timezone_choices": TIMEZONE_CHOICES,
        }
        return render(request, "main/settings.html", context)

    def post(self, request, *args, **kwargs):
        user = request.user
        
        sync_frequency = request.POST.get("sync_frequency")
        sync_preferred_hour = request.POST.get("sync_preferred_hour")
        theme = request.POST.get("theme")
        user_timezone = request.POST.get("user_timezone", "").strip()
        
        if sync_frequency in dict(user.SYNC_FREQUENCY_CHOICES):
            user.sync_frequency = sync_frequency
        
        try:
            hour = int(sync_preferred_hour)
            if 0 <= hour <= 23:
                user.sync_preferred_hour = hour
        except (ValueError, TypeError):
            pass
        
        if theme in dict(user.THEME_CHOICES):
            user.theme = theme

        if user_timezone and user_timezone in zoneinfo.available_timezones():
            user.user_timezone = user_timezone
        
        user.save()
        messages.success(request, "Settings saved successfully")
        return redirect("settings")


class DeleteUserAccountView(LoginRequiredMixin, View):
    """Delete the current user and all associated data."""
    login_url = "login"

    def post(self, request, *args, **kwargs):
        user = request.user
        username = user.username
        
        # Delete the user (CASCADE will delete all related data)
        user.delete()
        
        messages.success(request, "Your account and all associated data have been deleted.")
        return JsonResponse({"success": True})
