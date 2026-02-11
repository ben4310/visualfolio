import colorsys
import numpy as np


opacity = {"dark": 0.55, "light": 0.675}


def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))

def _rgb_to_hex(rgb_tuple: tuple[float, float, float]) -> str:
    r, g, b = [int(c * 255) for c in rgb_tuple]
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_hsl_components(hex_color):
    hex_color = hex_color.lstrip("#")

    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0

    h, l, s = colorsys.rgb_to_hls(r, g, b)

    return int(h * 360), int(s * 100), int(l * 100)


def humanize_number(value, fraction_point=1):
    """
    Converts float values to strings such as "10K", "200M" etc.
    """

    powers = [10**x for x in (12, 9, 6, 3, 0)]
    human_powers = ("T", "B", "M", "K", "")

    if not isinstance(value, float):
        value = float(value)

    for i, p in enumerate(powers):
        if value >= p:
            return_value = (
                str(
                    int(
                        round(value / (p / (10.0**fraction_point)))
                        / (10**fraction_point)
                    )
                )
                + human_powers[i]
            )
            break

    return return_value


def generate_shades(base_color, n, theme):
    """
    Generates n shades of the same hue (hue of base_color).
    """
    base_rgb = _hex_to_rgb(base_color)
    base_hue, _, _ = colorsys.rgb_to_hsv(*base_rgb)

    # Determine brightness range based on theme
    if theme == "light":
        min_brightness = 0.60
        max_brightness = 0.75

    elif theme == "dark":
        min_brightness = 0.3
        max_brightness = 0.45

    else:
        raise ValueError("Theme must be either 'light' or 'dark'.")

    # Generate shades of ascending brightness
    brightness_range = np.linspace(min_brightness, max_brightness, n)

    shades = []
    for brightness in brightness_range:
        if theme == "dark":
            # For darker ranges, since the lighter shades will appear most saturated,
            # increase saturation for brighter shades
            saturation = 1 - brightness + min_brightness -0.15  # Arbitrary correction
        else:
            # For lighter ranges, since the darker shades will appear most saturated,
            # increase saturation for brighter shades
            saturation = brightness + (1 - max_brightness)

        
        # Ensure the saturation is within [0,1] since changes in parameters can cause it to exceed this range
        saturation = max(0, min(1, saturation))

        rgb_color = colorsys.hls_to_rgb(base_hue, brightness, saturation)
        shade = _rgb_to_hex(rgb_color)
        shades.append(shade)

    return shades
