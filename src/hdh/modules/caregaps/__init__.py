"""Rule-based care-gap detection over the synthetic dataset.

Flags patients who are overdue for preventive care, have uncontrolled chronic
conditions without recent follow-up, missed a scheduled follow-up window, or
are seniors on many medications without a recent review.
"""

from .detector import CareGap, detect_gaps, reference_date
