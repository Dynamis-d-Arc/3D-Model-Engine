#include "TwinEditorUtils.h"

#include "GameFramework/Volume.h"

#if WITH_EDITOR
#include "ActorFactories/ActorFactory.h"
#include "Builders/CubeBuilder.h"
#include "NavigationSystem.h"
#include "NavMesh/NavMeshBoundsVolume.h"
#endif

bool UTwinEditorUtils::BuildBoxBrush(AVolume* Volume, FVector HalfExtent)
{
#if WITH_EDITOR
	if (!Volume)
	{
		return false;
	}
	UCubeBuilder* Builder = NewObject<UCubeBuilder>();
	Builder->X = HalfExtent.X * 2.0;
	Builder->Y = HalfExtent.Y * 2.0;
	Builder->Z = HalfExtent.Z * 2.0;
	UActorFactory::CreateBrushForVolumeActor(Volume, Builder);
	Volume->PostEditChange();
	if (UNavigationSystemV1* Nav = FNavigationSystem::GetCurrent<UNavigationSystemV1>(Volume->GetWorld()))
	{
		Nav->OnNavigationBoundsUpdated(Cast<ANavMeshBoundsVolume>(Volume));
	}
	return Volume->Brush != nullptr;
#else
	return false;
#endif
}
