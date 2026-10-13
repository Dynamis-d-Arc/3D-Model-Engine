#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"
#include "TwinGameMode.generated.h"

/** Default game mode of the walkthrough template (set in Config/DefaultEngine.ini and on every imported map). */
UCLASS()
class TWINWALKTHROUGH_API ATwinGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ATwinGameMode();
};
