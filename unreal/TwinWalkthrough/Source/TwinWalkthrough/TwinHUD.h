#pragma once

#include "CoreMinimal.h"
#include "GameFramework/HUD.h"
#include "TwinHUD.generated.h"

/** Canvas HUD: crosshair, current room, selected object (info panel + projected bounding box), help. */
UCLASS()
class TWINWALKTHROUGH_API ATwinHUD : public AHUD
{
	GENERATED_BODY()

public:
	virtual void DrawHUD() override;

private:
	void Panel(float X, float Y, float W, const TArray<FString>& Lines, const FLinearColor& Accent);
	void DrawBox(const FBox& Box, const FLinearColor& Color);
};
