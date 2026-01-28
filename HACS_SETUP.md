# HACS Validation Setup

## Remaining Manual Steps

The following HACS validation errors require manual configuration in GitHub repository settings:

### 1. ✅ Add Repository Topics

Go to: https://github.com/rdscoo1/atmeex_hacs/settings

Add the following topics:
- `home-assistant`
- `hacs`
- `integration`
- `atmeex`
- `airnanny`
- `ventilation`
- `climate-control`

**How to add:**
1. Go to repository main page
2. Click the gear icon ⚙️ next to "About" section
3. Add topics in the "Topics" field
4. Click "Save changes"

### 2. ✅ Enable Issues

Go to: https://github.com/rdscoo1/atmeex_hacs/settings

**How to enable:**
1. Scroll to "Features" section
2. Check the "Issues" checkbox
3. Settings save automatically

### 3. ⚠️ Brands Repository (Optional for Custom Repos)

This is only required if you want to submit to the official HACS default repository. For custom repositories installed via URL, this is **not required**.

If you want to add it later:
- Submit PR to: https://github.com/home-assistant/brands
- Follow: https://hacs.xyz/docs/publish/include#check-brands

## Verification

After completing steps 1 and 2, the HACS validation should pass. You can verify by:
1. Pushing a commit to trigger GitHub Actions
2. Checking the "HACS Validation" workflow results

## Current Status

✅ **Fixed in code:**
- Python 3.11 compatibility (FanEntityFeature.TURN_ON/TURN_OFF)
- Hassfest validation (manifest.json and translation keys)
- Translation keys now use valid format (a-z0-9-_)

⏳ **Requires manual action:**
- Add repository topics
- Enable Issues feature

❌ **Optional (not required for custom repos):**
- Brands repository submission
