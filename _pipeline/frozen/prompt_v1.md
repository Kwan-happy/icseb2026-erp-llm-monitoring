# Frozen Prompt — Hash Manifest

SHA-256 of every prompt file in `_pipeline/prompts/`; the prompt text itself lives only in those files.

| file | SHA-256 |
|---|---|
| `company_profile.txt` | `7a29381244795c4879741752f207a3ac2415970188b724d36c8c12c8d6820479` |
| `procurement_policy.txt` | `ebc236b5eae758c4cddcaed3eb69f42ab43691edb328290dc5d7ba749cd237c4` |
| `approval_matrix.txt` | `c16a8f84b742c78a591c03f8a70dbf2f38b881dd853c8434a6776a956726af9b` |
| `risk_rules.txt` | `b6ea34bed0495d218d55bd85df9b6ff7bdfe307a163c4f9d7596b8256fa8dd19` |
| `grain_po.txt` | `394a1e74250e3d5ccc95c8dba97d707523477c57edcba334eded6274a5db163a` |
| `grain_vendor.txt` | `00587ca8003b7945a0f88c46ec514769040799057df5ccebce3368e3fce47d13` |
| `grain_audit_B.txt` | `98981ed79a246b71dbb97931b77de2236aa9ea11669fa7b3358edf078db62049` |

## Assembly order per grain

Each request is the four shared files, in this order, followed by one grain file:

1. `company_profile.txt`
2. `procurement_policy.txt`
3. `approval_matrix.txt`
4. `risk_rules.txt`
5. grain file:
   - **po** (purchase-order grain): `grain_po.txt`
   - **vendor** (vendor grain): `grain_vendor.txt`
   - **audit** (vendor-audit-trail grain): `grain_audit_B.txt`
