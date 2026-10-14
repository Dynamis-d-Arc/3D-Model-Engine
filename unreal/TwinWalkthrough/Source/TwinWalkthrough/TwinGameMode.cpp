#include "TwinGameMode.h"

#include "EngineUtils.h"
#include "TwinCharacter.h"
#include "TwinHUD.h"
#include "TwinMeta.h"
#include "TwinPlayerController.h"
#include "TwinViewerDirector.h"

ATwinGameMode::ATwinGameMode()
{
	DefaultPawnClass = ATwinCharacter::StaticClass();
	PlayerControllerClass = ATwinPlayerController::StaticClass();
	HUDClass = ATwinHUD::StaticClass();
}

void ATwinGameMode::StartPlay()
{
	bool bImported = false;
	for (TActorIterator<AActor> It(GetWorld()); It && !bImported; ++It)
	{
		bImported = TwinMeta::IsTwin(*It);
	}
	if (!bImported && !ATwinViewerDirector::Get(GetWorld()))
	{
		// spawned before BeginPlay is dispatched, so every actor (and the controller) sees it in BeginPlay
		FActorSpawnParameters P;
		P.Name = TEXT("TwinViewerDirector");
		GetWorld()->SpawnActor<ATwinViewerDirector>(ATwinViewerDirector::StaticClass(), FTransform::Identity, P);
	}
	Super::StartPlay();
}
