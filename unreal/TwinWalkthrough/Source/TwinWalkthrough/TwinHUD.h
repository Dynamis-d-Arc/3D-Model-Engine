#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "TwinHUD.generated.h"

class ATwinPlayerController;
class ATwinViewerDirector;

/**
 * Canvas HUD: crosshair, current room, selected object (info panel + projected bounding box), help.
 * Runtime viewer additions: loading / error screens, model-check status, HVAC readings labelled as
 * measured or simulated, the airflow illustration banner + legend, room and building menus.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinHUD : public AHUD
{
	GENERATED_BODY()

public:
	virtual void DrawHUD() override;

private:
	float Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent);
	void DrawBox(const FBox& Box, const FLinearColor& Color);
	void DrawLoadScreen(ATwinPlayerController* PC, ATwinViewerDirector* D);
	void DrawMenu(ATwinPlayerController* PC, ATwinViewerDirector* D);
	void DrawAirflowBanner(ATwinViewerDirector* D);
	void HvacLines(ATwinViewerDirector* D, const AActor* Sel, TArray<FString>& Lines);
};
