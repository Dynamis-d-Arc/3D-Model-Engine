#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "TwinHUD.generated.h"

class ATwinPlayerController;
class ATwinViewerDirector;

/** One text line of a HUD panel (see ATwinHUD::LayoutPanel). */
struct FTwinHudLine
{
	FString Text;
	FLinearColor Color = FLinearColor(0.85f, 0.87f, 0.9f);
	float Scale = 0.95f;
	FLinearColor Badge = FLinearColor::Transparent;  // a small coloured square before the text (warnings, data source)
	FString BadgeGlyph;                               // drawn inside the square, e.g. "!"
	float Gap = 0.f;                                  // extra space above the line (starts a section)
	bool bRule = false;                               // a faint divider in that space
	float Indent = 0.f;                               // set by the layout for wrapped continuation lines

	FTwinHudLine() = default;
	FTwinHudLine(const FString& InText, const FLinearColor& InColor = FLinearColor(0.85f, 0.87f, 0.9f), float InScale = 0.95f)
		: Text(InText), Color(InColor), Scale(InScale)
	{
	}
};

/**
 * Canvas HUD for visitors who are not technical (building owners, facility managers):
 *   top left      building, room, size, flooring, storey, movement mode; short plain warnings when the model check
 *                 failed or the HVAC feed is broken;
 *   top centre    notifications, placed clear of the other panels; the airflow illustration banner + legend;
 *   bottom right  the selected object: a friendly type name, size, HVAC readings labelled LIVE BMS (measured) or
 *                 SIMULATED (demonstration, not measured);
 *   bottom centre a one-line hint bar of the main keys, or the full controls card (H; shown by itself for a few
 *                 seconds after a building loads) with the keys drawn as key caps.
 * I adds the technical details (model check numbers, data source, ids, object names) to the panels.
 * Runtime viewer: loading / error screens, room and building menus.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinHUD : public AHUD
{
	GENERATED_BODY()

public:
	virtual void DrawHUD() override;

private:
	/** Fixed-width panel of plain lines (the first one is the title); returns its height. */
	float Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent);
	/** Word-wraps the lines (in place) to fit MaxW and returns the panel size (width at least MinW). */
	FVector2D LayoutPanel(TArray<FTwinHudLine>& Lines, float MinW, float MaxW);
	/** Draws a laid-out panel (dark, translucent, coloured left accent) and reserves its rectangle for this frame. */
	FBox2D DrawPanel(float X, float Y, const FVector2D& Size, const TArray<FTwinHudLine>& Lines, const FLinearColor& Accent);
	/** One key cap; returns its width (bDraw = false only measures). */
	float KeyCap(float X, float Y, const FString& Label, bool bDraw);
	/** Key caps from a spec such as "W|A|S|D", "Ctrl|+|V", "PgUp|/|PgDn" ("+", "/", "or" are drawn as plain text). */
	float Keys(float X, float Y, const FString& Spec, bool bDraw);
	void DrawBox(const FBox& Box, const FLinearColor& Color);
	void DrawLoadScreen(ATwinPlayerController* PC, ATwinViewerDirector* D);
	void DrawMenu(ATwinPlayerController* PC, ATwinViewerDirector* D);
	FBox2D DrawInfoPanel(ATwinPlayerController* PC, ATwinViewerDirector* D);
	FBox2D DrawAirflowBanner(ATwinViewerDirector* D, int32 Floor, const FBox2D& Info);
	FBox2D DrawSelection(ATwinPlayerController* PC, ATwinViewerDirector* D, AActor* Sel, float Bottom);
	void HvacLines(ATwinViewerDirector* D, const AActor* Sel, TArray<FTwinHudLine>& Lines, TArray<FTwinHudLine>& Details);
	FBox2D DrawHintBar(ATwinPlayerController* PC, ATwinViewerDirector* D, float Y, float H);
	FBox2D DrawControlsCard(ATwinPlayerController* PC, ATwinViewerDirector* D, float Left, float Right, float Top, float Bottom);
	/** A centred notification, moved down below any panel it would cover. */
	void DrawToast(const FString& Message, const FLinearColor& Accent);

	TArray<FBox2D> Used;  // panels drawn this frame
};
