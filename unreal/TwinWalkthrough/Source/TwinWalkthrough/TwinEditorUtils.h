#pragma once

#include "CoreMinimal.h"
#include "Kismet/BlueprintFunctionLibrary.h"
#include "TwinEditorUtils.generated.h"

class AVolume;

/**
 * Editor helpers callable from Python (unreal.TwinEditorUtils.*) by the importer.
 * Volumes spawned from scripts have no brush geometry, so e.g. a NavMeshBoundsVolume would bound
 * nothing; BuildBoxBrush gives it a real box brush. No-ops (return false) outside the editor.
 */
UCLASS()
class TWINWALKTHROUGH_API UTwinEditorUtils : public UBlueprintFunctionLibrary
{
	GENERATED_BODY()

public:
	/** Replace the volume's brush with a box of the given half-extent (cm), centred on the actor. */
	UFUNCTION(BlueprintCallable, Category = "Twin|Editor")
	static bool BuildBoxBrush(AVolume* Volume, FVector HalfExtent);
};
