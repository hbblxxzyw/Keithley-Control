"""Numeric spin boxes that preserve small scientific-notation values."""

from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QDoubleSpinBox


def _clean_numeric_text(text: str, suffix: str = "") -> str:
    cleaned = str(text or "").strip()
    if suffix and cleaned.endswith(suffix):
        cleaned = cleaned[: -len(suffix)].strip()
    return (
        cleaned.replace(",", "")
        .replace("\u2212", "-")
        .replace("\uff0d", "-")
        .strip()
    )


def _format_exponent(value: float, significant_digits: int) -> str:
    text = f"{float(value):.{max(1, significant_digits) - 1}E}"
    mantissa, exponent = text.split("E", 1)
    mantissa = mantissa.rstrip("0").rstrip(".")
    exponent_value = int(exponent)
    return f"{mantissa}E{exponent_value:+d}".replace("E+", "E")


class ScientificDoubleSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that accepts and displays scientific notation reliably."""

    fixed_decimal_places = 5
    significant_digits = 10

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setKeyboardTracking(False)

    def textFromValue(self, value: float) -> str:
        value = float(value)
        abs_value = abs(value)

        decimals = max(0, min(self.decimals(), 12))
        fixed_text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        decimal_places = 0
        if "." in fixed_text:
            decimal_places = len(fixed_text.rsplit(".", 1)[1])

        if abs_value != 0.0 and (
            decimal_places > self.fixed_decimal_places or abs_value >= 1e6
        ):
            return _format_exponent(
                value,
                min(max(self.decimals(), 6), self.significant_digits),
            )

        return fixed_text or "0"

    def valueFromText(self, text: str) -> float:
        cleaned = _clean_numeric_text(text, self.suffix())
        if not cleaned:
            return self.minimum()
        return float(cleaned)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        cleaned = _clean_numeric_text(text, self.suffix())
        if not cleaned or cleaned in {"-", "+", ".", "-.", "+.", "E", "e"}:
            return (QValidator.State.Intermediate, text, pos)
        try:
            float(cleaned)
        except ValueError:
            partial_markers = ("e", "e+", "e-", "E", "E+", "E-")
            if cleaned.endswith(partial_markers):
                return (QValidator.State.Intermediate, text, pos)
            return (QValidator.State.Invalid, text, pos)
        return (QValidator.State.Acceptable, text, pos)

    def fixup(self, text: str) -> str:
        try:
            value = self.valueFromText(text)
        except Exception:
            value = self.minimum()
        value = min(max(value, self.minimum()), self.maximum())
        return self.textFromValue(value)


class AdaptiveDelaySpinBox(ScientificDoubleSpinBox):
    """Delay spinbox that stores ns-scale precision but keeps common values tidy."""

    display_decimals = 4

    def textFromValue(self, value: float) -> str:
        value = float(value)
        rounded_display_value = round(value, self.display_decimals)
        precision_tolerance = 0.5 * (10 ** -self.decimals())
        if abs(value - rounded_display_value) <= precision_tolerance:
            return f"{rounded_display_value:.{self.display_decimals}f}"
        return super().textFromValue(value)
