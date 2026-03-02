# CIPP API Documentation Theme

This documentation uses a custom theme matching the CIPP frontend application for consistent branding.

## Theme Configuration

### Color Palette

Based on the CIPP Material-UI theme (`src/theme/colors.js`):

**Primary Colors:**
- Orange (CyberDrain Brand): `#F77F00`
- Navy (CyberDrain Dark): `#003049`

**Status Colors:**
- Success: `#10B981` (Green)
- Warning: `#F79009` (Orange-yellow)
- Error: `#F04438` (Red)
- Info: `#06AED4` (Cyan)

**Neutral Palette:**
- 50: `#F9FAFB`
- 100: `#F2F4F7`
- 200: `#EAECF0`
- 300: `#D0D5DD`
- 400: `#98A2B3`
- 500: `#667085`
- 600: `#475467`
- 700: `#344054`
- 800: `#1D2939`
- 900: `#101828`

**HTTP Method Colors:**
- GET: `#10B981` (success green)
- POST: `#F77F00` (primary orange)
- PUT: `#06AED4` (info cyan)
- PATCH: `#9E77ED` (purple)
- DELETE: `#F04438` (error red)

### Typography

**Font Family:** `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`

Same as CIPP frontend (`src/theme/base/create-typography.js`):
- Base size: `16px`
- Line height: `1.6`
- Headings: Inter, 600 weight

### Layout

**Light Mode (default):**
- Background: `#F0F2F5` (CIPP high-contrast)
- Paper: `#FAFBFC`
- Text primary: `#101828`
- Text secondary: `#667085`

**Dark Mode (right panel):**
- Background: `#101826` (CIPP paper)
- Text: `#FFFFFF`

**Sidebar:**
- Background: `#003049` (CyberDrain Navy)
- Text: `#FFFFFF`
- Active text: `#F77F00` (CyberDrain Orange)
- Active background: `rgba(247, 127, 0, 0.15)` (Orange with transparency)

## Files

- `docs-template.hbs` - Custom HTML template with CIPP branding
- `.github/workflows/deploy-docs.yml` - GitHub Actions workflow with theme CLI flags
- `assets/` - Placeholder directory for favicons and images

## Customization

The theme is applied through Redocly CLI flags in the deployment workflow. To modify colors or typography, edit the `--theme.openapi.theme.*` flags in [.github/workflows/deploy-docs.yml](.github/workflows/deploy-docs.yml).

## Consistency with CIPP UI

This theme exactly matches:
- CIPP frontend color palette (`src/theme/colors.js`)
- CIPP typography settings (`src/theme/base/create-typography.js`)
- CIPP light mode palette (`src/theme/light/create-palette.js`)
- CIPP dark mode palette (`src/theme/dark/create-palette.js`)

This ensures users transitioning between the main CIPP application and API documentation experience consistent visual design.
