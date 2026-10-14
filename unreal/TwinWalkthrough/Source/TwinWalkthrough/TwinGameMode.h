#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "TwinGameMode.generated.h"

/**
 * Default game mode of the walkthrough template (set in Config/DefaultEngine.ini and on every imported map).
 * On a map without imported twin actors (the packaged viewer's L_Viewer) it adds an ATwinViewerDirector,
 * which loads the building from the backend at runtime.
 */
UCLASS()
class TWINWALKTHROUGH_API ATwinGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ATwinGameMode();
	virtual void StartPlay() override;
};
