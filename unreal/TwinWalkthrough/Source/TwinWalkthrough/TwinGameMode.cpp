#include "TwinGameMode.h"

#include "TwinCharacter.h"
#include "TwinHUD.h"
#include "TwinPlayerController.h"

ATwinGameMode::ATwinGameMode()
{
	DefaultPawnClass = ATwinCharacter::StaticClass();
	PlayerControllerClass = ATwinPlayerController::StaticClass();
	HUDClass = ATwinHUD::StaticClass();
}
